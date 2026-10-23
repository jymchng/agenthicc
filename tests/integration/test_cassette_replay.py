"""Hermetic replay of a recorded code_plan session.

The fixture in ``tests/fixtures/plan_mode/`` is a real 76-request recording of
a code_plan run: a rejected and then an approved plan review, an execution
phase that edits files and runs tests, a review, and a summary. Replaying it
drives the *current* code_plan state machine, agent turn, tool routing,
capability metadata and approval handling with the exact recorded model
responses, while every side-effecting built-in tool is replaced by a
schema-identical stub. A change to the phase graph, a transition tool, the tool
catalog, or a tool's accepted arguments makes the replay diverge from the
recording, and this test fails.

See the fixture README for the shape of the recording. Recording a new
cassette::

    uv run agenthicc --record-cassette tests/fixtures/<scenario>/
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.testing import SessionCassette, run_headless_replay

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent.parent / "fixtures" / "plan_mode"
CASSETTE = FIXTURE / "cassette.jsonl"
APPROVALS = FIXTURE / "approvals.jsonl"

#: The recording did not store its intent.
INTENT = "enhance this repo"

#: code_plan's transition tools, in the order an accepted run calls them.
ACCEPTED_TRANSITIONS = [
    "request_plan_approval",
    "finalize_plan",
    "mark_execute_complete",
    "approve_review",
]

#: Recorded calls that were already invalid when recorded (see fixture README).
KNOWN_RECORDED_MODEL_ERRORS = (
    "entry 30: read_file:",
    "entry 51: patch_file:",
    "entry 71: read_file:",
)

#: Write and exec calls made during execution. Each raises an approval prompt
#: in today's default Safe execute mode; the recording never raised them (see
#: the fixture README), so the replay's fallback answers them.
EXECUTE_PHASE_APPROVALS = 14


def _cassette() -> SessionCassette:
    return SessionCassette.from_path(CASSETTE, APPROVALS, intent=INTENT)


def test_recording_has_the_documented_shape() -> None:
    cassette = _cassette()

    assert len(cassette.entries) == 76
    assert [entry.index for entry in cassette.entries] == list(range(76))
    assert not any(entry.is_tool_free for entry in cassette.entries)
    valid_stops = {"end_turn", "tool_use", "max_tokens", "stop_sequence"}
    assert all(entry.response_stop_reason in valid_stops for entry in cassette.entries)
    assert cassette.entries[0].response_stop_reason == "tool_use"

    assert [(approval.kind, approval.allowed) for approval in cassette.approvals] == [
        ("plan_review", False),
        ("plan_review", True),
    ]
    assert all(approval.mode is None for approval in cassette.approvals)
    meta = json.loads((FIXTURE / "meta.json").read_text(encoding="utf-8"))
    assert meta["session_id"] == "cd1ba304-3ea8-47c7-bada-189462799692"


@pytest.mark.timeout(300)
async def test_code_plan_session_replays_hermetically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cassette = _cassette()

    result = await run_headless_replay(
        cassette,
        workflow="code_plan",
        cfg=AgenthiccConfig(),
        hermetic=True,
        mode="Yolo",
        timeout_s=240,
    )

    # Every recorded response was consumed by exactly the recorded phases.
    assert result.error is None
    assert result.status == "complete", result
    assert result.phases == ["plan", "execute", "review", "summarize"]
    assert result.transport_calls == len(cassette.entries) == 76
    assert result.skipped_entries == 0

    # The state machine accepted the recorded transitions in recorded order.
    accepted = [
        exchange.name
        for exchange in result.tool_exchanges
        if exchange.name in ACCEPTED_TRANSITIONS and exchange.accepted
    ]
    assert accepted == ACCEPTED_TRANSITIONS

    # Both recorded plan reviews were delivered, in order: the rejection with
    # its feedback, then the approval. The three other rejected requests are
    # the recorded calls that omitted the plan (entries 29, 32 and 33).
    reviews = [
        exchange for exchange in result.tool_exchanges if exchange.name == "request_plan_approval"
    ]
    assert [exchange.accepted for exchange in reviews] == [False, False, False, False, True]
    first, approved = reviews[0].result, reviews[-1].result
    assert isinstance(first, dict) and first["feedback"] == "be more extensive"
    assert isinstance(approved, dict) and str(approved["feedback"]).startswith("Looks beautiful!")
    assert all("missing 1 required positional argument" in str(r.result) for r in reviews[1:4])

    # Plan reviews came from the recording; only execution prompts did not.
    assert result.approvals_consumed == len(cassette.approvals) + EXECUTE_PHASE_APPROVALS
    assert result.approvals_unrecorded == EXECUTE_PHASE_APPROVALS

    # No recorded call was blocked by the replay's capability policy.
    assert not any(
        "blocked in" in str(exchange.result)
        for exchange in result.tool_exchanges
        if not exchange.accepted
    )

    # Every recorded call still routes to an offered tool with arguments its
    # current signature accepts, except the model errors made when recorded.
    assert len(result.contract_violations) == len(KNOWN_RECORDED_MODEL_ERRORS), (
        result.contract_violations
    )
    for violation, known in zip(
        sorted(result.contract_violations), sorted(KNOWN_RECORDED_MODEL_ERRORS), strict=True
    ):
        assert violation.startswith(known), violation

    # Hermetic: the recorded edits and test runs never touched the filesystem.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.timeout(300)
async def test_plan_mode_replay_keeps_execution_inside_the_hermetic_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Plan-mode session still executes only stubs after its mode switch.

    The execute phase switches from the caller's Plan mode to the approved
    Safe mode. Its write and exec tools must be offered under that mode, and
    must be the replay stubs, never the real built-ins behind them.
    """
    monkeypatch.chdir(tmp_path)
    cassette = _cassette()

    result = await run_headless_replay(
        cassette,
        workflow="code_plan",
        cfg=AgenthiccConfig(),
        hermetic=True,
        mode="Plan",
        timeout_s=240,
    )

    assert result.status == "complete", result
    assert result.phases == ["plan", "execute", "review", "summarize"]
    side_effecting = {
        "write_file",
        "patch_file",
        "apply_diff",
        "append_file",
        "run_tests",
        "run_bash",
    }
    execute_calls = [
        exchange
        for exchange in result.tool_exchanges
        if exchange.name in side_effecting and exchange.accepted
    ]
    assert execute_calls
    assert all(
        isinstance(exchange.result, dict) and exchange.result.get("replayed") is True
        for exchange in execute_calls
    )
    assert list(tmp_path.iterdir()) == []


async def test_unknown_workflow_is_rejected_before_any_request() -> None:
    with pytest.raises(ValueError, match="unknown workflow"):
        await run_headless_replay(_cassette(), workflow="no_such_workflow", cfg=AgenthiccConfig())
