"""MockApprovalService — headless ApprovalService for cassette replay tests.

Consumes pre-recorded :class:`~agenthicc.testing.cassette.ApprovalEntry`
objects in order; falls back to auto-approve when the queue is exhausted.

Usage::

    from agenthicc.testing import SessionCassette

    cassette = SessionCassette.from_path("tests/fixtures/plan_mode/cassette.jsonl",
                                         approvals_path="tests/fixtures/plan_mode/approvals.jsonl")
    mock_approval = cassette.to_mock_approval_service()
    # Pass to run_headless_replay(cassette, approval_svc=mock_approval)
"""
from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.testing.cassette import ApprovalEntry
    from agenthicc.tools.approval import ApprovalRequest, ApprovalResponse


class MockApprovalService:
    """Headless drop-in for :class:`~agenthicc.tools.approval.ApprovalService`.

    Dequeues :class:`~agenthicc.testing.cassette.ApprovalEntry` objects in
    insertion order.  When the queue is exhausted, every subsequent request
    is auto-approved so the workflow can run to completion even if the cassette
    was shorter than the live session.
    """

    def __init__(self, entries: list[ApprovalEntry]) -> None:
        from agenthicc.tools.approval import ApprovalResponse  # noqa: PLC0415
        self._queue: deque[ApprovalEntry] = deque(entries)
        self._consumed: int = 0

    async def request_approval(self, req: ApprovalRequest) -> ApprovalResponse:
        from agenthicc.tools.approval import ApprovalResponse  # noqa: PLC0415
        if self._queue:
            entry = self._queue.popleft()
            self._consumed += 1
            return ApprovalResponse(
                allowed=entry.allowed,
                message=entry.message,
                remember=entry.remember,
                remember_all=entry.remember_all,
            )
        # Queue exhausted — auto-approve so replay completes gracefully.
        self._consumed += 1
        return ApprovalResponse(
            allowed=True,
            message="(auto-approved: cassette exhausted)",
        )

    def respond(self, response: ApprovalResponse) -> None:
        pass  # no-op: no TUI overlay in headless mode

    def reset_turn_memory(self) -> None:
        pass  # no per-turn memory in headless mode

    @property
    def consumed(self) -> int:
        """Number of approval requests consumed so far."""
        return self._consumed
