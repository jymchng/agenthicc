"""Cassette-based regression tests — verifies workflow orchestration.

How to create a cassette
------------------------
Run a session with ``--record-cassette`` pointing to a directory::

    uv run agenthicc --record-cassette ~/.agenthicc/sessions/<id>/cassette/

Or specify any directory you like::

    uv run agenthicc --record-cassette /tmp/my-cassette/

Three files are written:
- ``cassette.jsonl``  — one JSON line per LLM call (request summary + response)
- ``approvals.jsonl`` — one JSON line per approval gate (allowed/rejected + message)
- ``meta.json``       — session_id, recorded_at

Copy those files into ``tests/fixtures/<scenario>/`` and reference them in
the tests below.  The cassette is fully self-contained — no live API keys
are needed to run the tests.

Skipping when fixtures are absent
----------------------------------
The tests in this file are decorated with a path-existence check so they
skip cleanly when the fixture directory does not yet exist (e.g. in CI
before the first cassette is recorded).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenthicc.testing import (
    ReplayResult,
    SessionCassette,
    run_headless_replay,
)

pytestmark = pytest.mark.integration

# ── fixture paths — copy your cassette files here ────────────────────────────

FIXTURES = Path(__file__).parent.parent / "fixtures"

PLAN_MODE_CASSETTE  = FIXTURES / "plan_mode" / "cassette.jsonl"
PLAN_MODE_APPROVALS = FIXTURES / "plan_mode" / "approvals.jsonl"
PLAN_MODE_META      = FIXTURES / "plan_mode" / "meta.json"


def _intent_from_meta(meta_path: Path, fallback: str = "enhance this repo") -> str:
    if meta_path.exists():
        try:
            return str(json.loads(meta_path.read_text())["intent"]) or fallback
        except Exception:  # noqa: BLE001
            pass
    return fallback


# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.cassette
@pytest.mark.skipif(
    not PLAN_MODE_CASSETTE.exists(),
    reason=(
        "Plan-mode cassette not found.  Record one with:\n"
        "  uv run agenthicc --record-cassette tests/fixtures/plan_mode/\n"
        "Then re-run: uv run pytest tests/integration/test_cassette_replay.py"
    ),
)
@pytest.mark.timeout(600)
async def test_plan_mode_end_to_end_orchestration() -> None:
    """Verify that the code_plan workflow still routes through all four phases.

    What this catches:
    - Missing tools (finalize_plan removed from plan phase tool list)
    - Broken state machine transitions (review → execute instead of → summarize)
    - Approval gate removed (workflow skips plan approval)
    - Max-iterations regression (phase hits cap before calling completion tool)
    """
    cassette = SessionCassette.from_path(
        cassette_path=PLAN_MODE_CASSETTE,
        approvals_path=PLAN_MODE_APPROVALS if PLAN_MODE_APPROVALS.exists() else None,
        intent=_intent_from_meta(PLAN_MODE_META),
    )

    result: ReplayResult = await run_headless_replay(cassette)

    # ── structural assertions ─────────────────────────────────────────────────
    assert result.status == "complete", (
        f"Workflow did not reach 'complete'.  Status: {result.status!r}. "
        f"Error: {result.error!r}.  Phases completed: {result.phases}"
    )
    assert result.phases == ["plan", "execute", "review", "summarize"], (
        f"Phase sequence mismatch: {result.phases}"
    )

    # ── tool-call assertions ──────────────────────────────────────────────────
    assert "finalize_plan" in result.tools_called, (
        "finalize_plan was not called — plan approval gate may be broken or tool missing"
    )
    assert "mark_execute_complete" in result.tools_called, (
        "mark_execute_complete was not called — execute phase may not be completing"
    )
    assert "approve_review" in result.tools_called, (
        "approve_review was not called — review phase may not be reaching completion"
    )

    # ── approval-gate assertions ──────────────────────────────────────────────
    assert result.approvals_consumed >= 1, (
        "No approval gates were consumed — plan approval overlay may be broken"
    )

    # ── transport-call sanity ─────────────────────────────────────────────────
    assert result.transport_calls == len(cassette.entries), (
        f"Transport call count mismatch: made {result.transport_calls} calls, "
        f"cassette has {len(cassette.entries)} entries.  "
        "The orchestration is doing a different number of LLM calls than when recorded."
    )

    # Summarise what happened (always printed, useful on CI)
    print(
        f"\n  phases={result.phases}"
        f"\n  tools_called={result.tools_called}"
        f"\n  approvals_consumed={result.approvals_consumed}"
        f"\n  transport_calls={result.transport_calls}"
    )


@pytest.mark.cassette
@pytest.mark.skipif(
    not PLAN_MODE_CASSETTE.exists(),
    reason="Plan-mode cassette not found — see test_plan_mode_end_to_end_orchestration.",
)
@pytest.mark.timeout(30)
async def test_plan_mode_cassette_integrity() -> None:
    """Verify the cassette file itself is well-formed before running the main test."""
    cassette = SessionCassette.from_path(cassette_path=PLAN_MODE_CASSETTE)

    assert len(cassette.entries) > 0, "cassette.jsonl is empty"

    # All four stop reasons defined by the Transport protocol are valid.
    valid_stop_reasons = {"end_turn", "tool_use", "max_tokens", "stop_sequence"}
    bad = [
        f"  index={e.index}  stop_reason={e.response_stop_reason!r}"
        for e in cassette.entries
        if e.response_stop_reason not in valid_stop_reasons
    ]
    assert not bad, (
        f"{len(bad)} entries have unrecognised stop_reason:\n" + "\n".join(bad)
    )

    # The first response should be a tool use (LLM calls request_plan_approval or similar)
    first = cassette.entries[0]
    assert first.response_stop_reason == "tool_use", (
        f"Expected first response to be tool_use, got {first.response_stop_reason!r}"
    )


# ── custom cassette path example ──────────────────────────────────────────────
#
# The pattern below shows how to reference a cassette by an explicit path.
# Duplicate / rename this block for each scenario you want to regression-test.
#
# @pytest.mark.skipif(
#     not Path("tests/fixtures/my_scenario/cassette.jsonl").exists(),
#     reason="my_scenario cassette not found",
# )
# async def test_my_scenario() -> None:
#     cassette = SessionCassette.from_path(
#         cassette_path="tests/fixtures/my_scenario/cassette.jsonl",
#         approvals_path="tests/fixtures/my_scenario/approvals.jsonl",
#         intent="refactor the auth module",
#     )
#     result = await run_headless_replay(cassette)
#     assert result.status == "complete"
#     assert "finalize_plan" in result.tools_called
