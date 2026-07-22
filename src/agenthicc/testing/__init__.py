"""agenthicc.testing — cassette-based regression test utilities.

Public API::

    from agenthicc.testing import (
        SessionCassette,
        MockApprovalService,
        run_headless_replay,
        ReplayResult,
    )

    # Load cassette from explicit paths (copy your cassette files into tests/fixtures/)
    cassette = SessionCassette.from_path(
        cassette_path="tests/fixtures/plan_mode/cassette.jsonl",
        approvals_path="tests/fixtures/plan_mode/approvals.jsonl",
        intent="enhance this repo",
    )
    result = await run_headless_replay(cassette)

    assert result.status == "complete"
    assert "finalize_plan" in result.tools_called

Recording is wired into the TUI session via ``--record-cassette <dir>``::

    uv run agenthicc --record-cassette ~/.agenthicc/sessions/<id>/cassette/
"""

from __future__ import annotations

from agenthicc.testing.cassette import SessionCassette, CassetteEntry, ApprovalEntry
from agenthicc.testing.mock_approval import MockApprovalService
from agenthicc.testing.headless import ReplayResult, run_headless_replay

__all__ = [
    "SessionCassette",
    "CassetteEntry",
    "ApprovalEntry",
    "MockApprovalService",
    "ReplayResult",
    "run_headless_replay",
]
