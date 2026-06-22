"""Run resumption coordinator (PRD-129 Phase 3).

On session resume, detects a turn that was interrupted mid-flight — a process
crash, a kill, an unhandled error — and prepares everything needed to re-drive
it from where it left off:

- the user message to re-submit,
- the rollback point (pre-turn message count), and
- a durable idempotency ledger seeded with the tools that already ran, so their
  side effects are replayed rather than repeated.

Resuming reuses the turn's original ``turn_id`` so the seeded ledger and the
journal's turn markers line up; a successful re-drive writes ``turn_completed``,
closing the turn for good.
"""

from __future__ import annotations

from dataclasses import dataclass

from agenthicc.memory.journal import ConversationJournal, IncompleteTurn
from agenthicc.runners.durable_ledger import DurableIdempotencyLedger

__all__ = ["RunCoordinator", "ResumePlan"]


@dataclass(frozen=True)
class ResumePlan:
    """Everything needed to re-drive an interrupted turn."""

    turn_id: str
    user_message: str
    base_count: int
    ledger: DurableIdempotencyLedger


class RunCoordinator:
    """Detects and plans the resumption of interrupted turns from the journal."""

    @staticmethod
    def detect_incomplete_turn(journal: ConversationJournal) -> IncompleteTurn | None:
        """Return the turn to resume, or ``None`` if the session ended cleanly."""
        return journal.resume_state()

    @staticmethod
    def build_resume_plan(
        journal: ConversationJournal, incomplete: IncompleteTurn
    ) -> ResumePlan:
        """Build a :class:`ResumePlan` with a ledger seeded from prior records."""
        ledger = DurableIdempotencyLedger(
            journal,
            incomplete.turn_id,
            seed_records=incomplete.tool_records,
        )
        return ResumePlan(
            turn_id=incomplete.turn_id,
            user_message=incomplete.user_message,
            base_count=incomplete.base_count,
            ledger=ledger,
        )
