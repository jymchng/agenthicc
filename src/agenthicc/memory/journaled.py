"""Journaled ShortTermMemory (PRD-129 Phase 2).

A :class:`~lauren_ai._memory.ShortTermMemory` whose every append and reset is
mirrored to a durable :class:`~agenthicc.memory.journal.ConversationJournal`.
This makes the live conversation a crash-recoverable *projection* of the
journal: on construction it folds any existing journal content back into memory
(transparent resume), and thereafter every transition is durably recorded
before the next LLM call.

The append/reset mutation surface and tool-exchange lifecycle are journaled.
``trim_to_fit`` is an in-RAM projection operation and is deliberately not
journaled because the full history remains durable.  Explicit
``ensure_valid_and_persist`` recovery is journaled: a repaired interrupted
tool-call tail is part of the next turn's durable context and must survive a
process restart.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal

from lauren_ai import ToolConversationIntegrityError
from lauren_ai._memory import ShortTermMemory, ToolHistoryIssue, ToolHistoryReport

if TYPE_CHECKING:
    from lauren_ai._memory import ToolExchange, ToolResultRecord

from agenthicc.memory.journal import ConversationJournal, tool_exchange_recovery_event_id
from agenthicc.memory.tool_history import (
    ToolHistoryRecoveryRequired,
    ToolHistoryRecoveryState,
    repair_non_adjacent_tool_history,
)

__all__ = ["JournaledShortTermMemory"]


def _tool_call_id_hash(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _repairable_tool_call_hashes(report: ToolHistoryReport) -> frozenset[str]:
    """Hash uniquely identified unanswered calls reported as missing or late.

    The persisted assistant ``tool_use`` block is itself durable evidence that
    the model requested a tool. If its result is missing after a process
    interruption, recovery may add an explicit *interrupted* error result; it
    must never invent successful tool output. Lauren-ai may classify an
    unanswered call as ``non_adjacent_results`` when an unrelated result occurs
    later in the transcript, so both validator issue codes are considered.
    Duplicate call IDs are excluded because they cannot be paired
    unambiguously.
    """
    recovered: set[str] = set()
    for issue in report.issues:
        if issue.code not in {"missing_results", "non_adjacent_results"}:
            continue
        expected = issue.expected_ids
        observed = set(issue.observed_ids)
        counts: dict[str, int] = {}
        for call_id in expected:
            if isinstance(call_id, str) and call_id:
                counts[call_id] = counts.get(call_id, 0) + 1
        recovered.update(
            _tool_call_id_hash(call_id)
            for call_id, count in counts.items()
            if count == 1 and call_id not in observed
        )
    return frozenset(recovered)


class JournaledShortTermMemory(ShortTermMemory):
    """``ShortTermMemory`` that durably journals every append and reset."""

    def __init__(self, journal: ConversationJournal, max_tokens: int = 40_000) -> None:
        super().__init__(max_tokens=max_tokens)
        self._journal = journal
        self._tool_history_recovery = ToolHistoryRecoveryState()
        # Resume: fold any existing journal content into memory *without*
        # re-journaling it (these entries are already durable on disk).
        msgs, summary = journal.fold()
        if msgs or summary is not None:
            self._messages = list(msgs)
            self._summary = summary
        corrupt_count = journal.corrupt_trailing_records
        if corrupt_count:
            self._tool_history_recovery = ToolHistoryRecoveryState(
                "journal_corrupt",
                (f"corrupt_trailing_records:{min(corrupt_count, 1_000_000)}",),
                False,
            )
            return
        # A queued continuation can be journaled before a late result from an
        # interrupted task. Repair only exact, uniquely correlated IDs. A
        # repair failure is represented as blocked provider readiness so the
        # durable session remains available for inspection.
        self.retry_tool_history_recovery()

    # ── append surface — record every newly-added message ────────────────────

    def _journal_new(self, before: int) -> None:
        """Journal every message appended since the buffer had *before* items."""
        for msg in self._messages[before:]:
            self._journal.append_message(msg)

    def add_user(self, content: str | list[object]) -> None:
        before = len(self._messages)
        super().add_user(content)
        self._journal_new(before)

    def add_assistant(self, completion: object) -> None:
        before = len(self._messages)
        super().add_assistant(completion)
        self._journal_new(before)

    def add_tool_result(self, result: object) -> None:
        before = len(self._messages)
        super().add_tool_result(result)
        self._journal_new(before)

    def add_tool_results(self, results: list[object]) -> None:
        if len(results) == 1:
            self.add_tool_result(results[0])
            return
        before = len(self._messages)
        super().add_tool_results(results)
        self._journal_new(before)

    # ── reset surface — retry rollback and compaction ────────────────────────

    def snapshot(self) -> dict[str, object]:
        """Return a copy-on-write checkpoint of the projection (AGX-12).

        Messages in this memory are values: appends add new objects, and
        repair, compaction and trimming build new lists from copies instead of
        editing a message in place. A checkpoint therefore keeps references to
        the current messages rather than deep-copying the whole history on
        every provider step; ``restore`` rebuilds the list from them. The
        active tool exchange stays a deep copy: it is small and holds the
        calls' input mappings.
        """
        checkpoint: dict[str, object] = {
            "messages": list(self._messages),
            "summary": self._summary,
        }
        if self._active_exchange is not None:
            checkpoint["tool_exchange"] = copy.deepcopy(self._active_exchange)
        return checkpoint

    def restore(
        self,
        data: object,
        *,
        turn_id: str = "",
        step_id: str = "",
        reason: str = "",
    ) -> None:
        """Restore a projection and record its optional recovery scope."""
        super().restore(data)
        self._journal.reset(
            self._messages,
            self._summary,
            turn_id=turn_id,
            step_id=step_id,
            reason=reason,
        )

    def commit_tool_exchange(
        self,
        exchange: "ToolExchange",
        results: list[object],
        *,
        on_unresolved: str = "synthesize_error_results",
    ) -> "ToolExchange":
        """Reject a late result from an exchange no longer owned by memory.

        Lauren-ai validates the exchange payload, but 1.5.0 still permits a
        started exchange to commit when ``restore()`` has already cleared the
        active owner. That is unsafe after a cancellation/resume race: the
        stale task would append its result after the queued continuation.
        """
        from lauren_ai import ToolConversationIntegrityError

        current = self.active_tool_exchange
        if current is None or current.exchange_id != exchange.exchange_id:
            expected_count = len(current.call_ids) if current is not None else 0
            raise ToolConversationIntegrityError(
                "Tool exchange does not own the active transaction",
                code="exchange_owner_mismatch",
                expected_count=expected_count,
                observed_count=len(exchange.call_ids),
            )
        return super().commit_tool_exchange(
            exchange,
            results,
            on_unresolved=on_unresolved,
        )

    def journal_reset(self) -> None:
        """Record the current full state.

        Called by the compactor after it replaces ``_messages`` in place (which
        bypasses the append/restore overrides), keeping the journal in sync.
        """
        self._journal.reset(self._messages, self._summary)

    def replace_state(self, messages: Iterable[object], summary: str | None) -> None:
        """Durably replace the projection before publishing it in memory.

        Lauren-ai 1.6.0 does not expose this hook yet, so defining it here also
        gives Agenthicc's compactor a transactional boundary on the minimum
        supported dependency. The journal writes a prepare/candidate/commit
        sequence; folding ignores an uncommitted candidate and restores the
        exact prior projection after a crash.
        """
        candidate = copy.deepcopy(list(messages))
        active_exchange = self.active_tool_exchange
        if active_exchange is not None and active_exchange.state == "started":
            from lauren_ai import ToolConversationIntegrityError

            raise ToolConversationIntegrityError(
                "Cannot replace conversation state during an active tool exchange",
                code="compaction_during_tool_exchange",
                expected_count=len(active_exchange.call_ids),
                observed_count=0,
            )

        self._journal.replace_state(candidate, summary)
        # Publish only after the durable transaction succeeds. This mirrors
        # lauren-ai's base replacement contract while retaining compatibility
        # with 1.6.0, where that method is not yet present.
        self._messages = candidate
        self._summary = summary

    # ── provider-step recovery contract (PRD-182) ─────────────────────────

    def begin_logical_turn(
        self,
        turn_id: str,
        user_message: str,
        *,
        conversation_id: str = "",
    ) -> None:
        """Record a logical-turn boundary without copying conversation data."""
        self._journal.turn_started(
            turn_id,
            user_message,
            len(self._messages),
            conversation_id=conversation_id,
            base_cursor=self._journal.cursor,
        )

    def begin_provider_step(
        self,
        turn_id: str,
        step_id: str,
        attempt_id: str,
        *,
        step_index: int,
    ) -> object:
        """Record an attempt and return its attempt-local memory checkpoint."""
        self._journal.step_started(
            turn_id,
            step_id,
            attempt_id,
            step_index=step_index,
            base_cursor=self._journal.cursor,
        )
        return self.snapshot()

    def commit_provider_step(
        self,
        turn_id: str,
        step_id: str,
        *,
        step_index: int,
        message_count: int | None = None,
    ) -> None:
        """Record a step only after its message projection is complete."""
        self._journal.step_committed(
            turn_id,
            step_id,
            step_index=step_index,
            cursor=self._journal.cursor,
            message_count=len(self._messages) if message_count is None else message_count,
        )

    def rollback_uncommitted_attempt(
        self,
        checkpoint: object,
        *,
        turn_id: str = "",
        step_id: str = "",
    ) -> None:
        """Restore one attempt checkpoint and journal its bounded scope.

        Callers should pass a checkpoint returned by
        :meth:`begin_provider_step`; the provider-step runner, rather than a
        phase or logical-turn retry loop, owns this operation.
        """
        self.restore(
            checkpoint,
            turn_id=turn_id,
            step_id=step_id,
            reason="provider_attempt_rollback",
        )

    def record_partial_fragment(
        self,
        turn_id: str,
        text: str,
        *,
        step_id: str = "",
        attempt_id: str = "",
    ) -> None:
        """Persist bounded interrupted output outside provider message memory."""
        self._journal.partial_fragment(
            turn_id,
            step_id=step_id,
            attempt_id=attempt_id,
            text=text,
        )

    def finalize_turn_failure(
        self,
        turn_id: str,
        *,
        last_committed_step: str = "",
        error_kind: str = "error",
        retryable: bool = False,
    ) -> None:
        """Close a failed turn while retaining its committed projection."""
        self._journal.turn_failed(
            turn_id,
            last_committed_step=last_committed_step,
            cursor=self._journal.cursor,
            error_kind=error_kind,
            retryable=retryable,
        )

    def rollback_to(self, count: int) -> None:
        """Truncate to *count* messages and journal the reset (PRD-129 Phase 3).

        Retained for legacy callers that explicitly request a historical
        rollback. Provider-step recovery must not call this method with a
        logical turn's ``base_count``: doing so would erase messages from
        already committed steps. Use ``rollback_uncommitted_attempt`` for the
        current provider attempt instead.
        """
        self._messages = self._messages[:count]
        self._journal.reset(self._messages, self._summary)

    def ensure_valid_and_persist(self) -> bool:
        """Heal an interrupted provider tail and persist the healed state.

        ``ShortTermMemory.ensure_valid()`` inserts synthetic interruption
        results for unanswered tool calls, but the base implementation does
        not journal that in-place repair. That is normally correct for a
        transient pre-send repair. An explicit user cancellation is different:
        the repaired history is the context the next turn must remember, so it
        must survive a process restart as well.

        lauren-ai runs this before every provider step. A valid history — the
        common case — returns before anything is copied: lauren-ai's repair
        returns a valid history unchanged, so the copy, repair and comparison
        below could only conclude ``False`` (AGX-12).
        """
        if self.validate_tool_history().ok:
            return False
        before = copy.deepcopy(self._messages)
        candidate = copy.copy(self)
        candidate._messages = copy.deepcopy(before)
        candidate._active_exchange = None
        candidate.ensure_valid()
        report = candidate.validate_tool_history()
        if not report.ok:
            from agenthicc.memory.tool_history import _integrity_error

            raise _integrity_error(report)
        changed = candidate._messages != before
        if changed:
            self.commit_repaired_tool_history(candidate._messages, self._summary)
        return changed

    @property
    def tool_history_recovery(self) -> ToolHistoryRecoveryState:
        """Return the redacted provider-readiness state of this session history."""
        return self._tool_history_recovery

    def retry_tool_history_recovery(self) -> ToolHistoryRecoveryState:
        """Retry only deterministic local repair; never contact a provider."""
        if self._tool_history_recovery.status == "journal_corrupt":
            # fold_path deliberately stops at the first malformed JSON record.
            # Treating that prefix as a complete transcript could silently
            # discard valid later records, so automatic repair must not clear
            # this state or make the prefix provider-eligible.
            return self._tool_history_recovery
        try:
            original = copy.deepcopy(self._messages)
            candidate = copy.copy(self)
            candidate._messages = copy.deepcopy(original)
            candidate._active_exchange = None
            pending_call_hashes = self._journal.fold_pending_tool_exchange_call_hashes()
            source_report = candidate.validate_tool_history()
            # A crash may occur after the assistant tool-call message is
            # journaled but before tool_exchange_started is. The exact,
            # uniquely identified missing call in the persisted transcript is
            # sufficient evidence to record an explicit interruption result;
            # requiring the later lifecycle receipt left this narrow crash
            # window permanently blocked. This never fabricates tool output.
            repairable_missing_hashes = pending_call_hashes | _repairable_tool_call_hashes(
                source_report
            )
            repair_non_adjacent_tool_history(
                candidate,
                commit=False,
                allowed_missing_result_hashes=repairable_missing_hashes,
            )
            # Lauren-ai's canonical repair synthesizes only an error result
            # for an unanswered call whose exact ID was present in the source
            # validator report (or in the unfinished-exchange receipt).
            # Duplicate, orphan, malformed, and otherwise ambiguous history
            # still fails the final full validation below.
            before_heal = candidate.validate_tool_history()
            for issue in before_heal.issues:
                if issue.code != "missing_results":
                    continue
                expected = issue.expected_ids
                observed_ids = set(issue.observed_ids)
                missing_ids = [value for value in expected if value not in observed_ids]
                if not missing_ids or any(
                    _tool_call_id_hash(value) not in repairable_missing_hashes
                    for value in missing_ids
                ):
                    from agenthicc.memory.tool_history import _integrity_error

                    raise _integrity_error(before_heal)
            # Lauren-ai's canonical deterministic repair handles a uniquely
            # identified missing result after non-adjacent results have been
            # reordered. Both operations stay on this detached projection.
            candidate.ensure_valid()
            report = candidate.validate_tool_history()
            if not report.ok:
                from agenthicc.memory.tool_history import _integrity_error

                raise _integrity_error(report)
            changed = candidate._messages != original
            if changed:
                self.commit_repaired_tool_history(candidate._messages, self._summary)
        except Exception as exc:
            self._tool_history_recovery = self._blocked_recovery_state(
                exc,
                repair_attempted=True,
            )
            return self._tool_history_recovery
        self._tool_history_recovery = ToolHistoryRecoveryState(
            "repaired" if changed else "ready",
            (),
            True,
        )
        return self._tool_history_recovery

    def ensure_provider_ready(self) -> bool:
        """Repair/validate history before any model request.

        Returns true when this call made a deterministic repair. Invalid state
        remains blocked and raises a typed, redacted error before provider I/O.
        """
        if not self._tool_history_recovery.provider_ready:
            state = self.retry_tool_history_recovery()
            if not state.provider_ready:
                raise ToolHistoryRecoveryRequired(state)
            return state.status == "repaired"

        report = self.validate_tool_history()
        if report.ok:
            return False
        state = self.retry_tool_history_recovery()
        if not state.provider_ready:
            raise ToolHistoryRecoveryRequired(state)
        return state.status == "repaired"

    def commit_repaired_tool_history(
        self,
        messages: list[object],
        summary: str | None,
    ) -> None:
        """Validate and atomically commit a complete repaired projection.

        This is the journal boundary used by deterministic tool-history repair.
        The existing projection is not published or altered unless the full
        Lauren-ai validator accepts the candidate and its journal transaction
        commits.
        """
        candidate_messages = copy.deepcopy(messages)
        candidate = copy.copy(self)
        candidate._messages = candidate_messages
        candidate._summary = summary
        candidate._active_exchange = None
        report = candidate.validate_tool_history()
        if not report.ok:
            from agenthicc.memory.tool_history import _integrity_error

            raise _integrity_error(report)

        self._journal.replace_state(candidate_messages, summary)
        self._messages = candidate_messages
        self._summary = summary
        active_exchange = self.active_tool_exchange
        if active_exchange is not None and active_exchange.state == "started":
            self._abort_repaired_exchange(active_exchange)
        # Clear the stale in-memory owner. A late tool task must receive the
        # existing exchange_owner_mismatch diagnostic rather than append to
        # the recovered conversation.
        self._active_exchange = None

    def _blocked_recovery_state(
        self,
        error: BaseException,
        *,
        repair_attempted: bool,
    ) -> ToolHistoryRecoveryState:
        """Summarize a failed repair without copying message contents to UI state."""
        issues: tuple[ToolHistoryIssue, ...] = ()
        codes: tuple[str, ...] = ()
        try:
            report = self.validate_tool_history()
            issues = report.issues
            codes = tuple(dict.fromkeys(issue.code[:64] for issue in issues))
        except Exception:
            codes = ()
        error_code = error.code if isinstance(error, ToolConversationIntegrityError) else None
        if isinstance(error, OSError):
            error_code = "journal_write_failed"
        if isinstance(error_code, str) and error_code and error_code not in codes:
            codes = (*codes, error_code[:64])
        if not codes:
            codes = ("history_repair_failed",)
        status: Literal["repair_required", "irrecoverable", "journal_corrupt"]
        if error_code == "journal_corrupt":
            status = "journal_corrupt"
        elif issues and all(not issue.repairable for issue in issues):
            status = "irrecoverable"
        else:
            status = "repair_required"
        return ToolHistoryRecoveryState(status, codes[:8], repair_attempted)

    def on_tool_exchange_started(self, exchange: ToolExchange) -> None:
        """Persist safe lifecycle metadata for an in-flight exchange."""
        self._journal.tool_exchange_started(
            exchange.exchange_id,
            run_id=exchange.run_id,
            call_count=len(exchange.call_ids),
            call_ids=list(exchange.call_ids),
        )

    def on_tool_exchange_committed(self, exchange: ToolExchange) -> None:
        """Persist the exchange commit after its message projection is durable."""
        synthetic_count = sum(1 for outcome in exchange.outcomes if outcome.synthetic)
        self._journal.tool_exchange_committed(
            exchange.exchange_id,
            call_count=len(exchange.call_ids),
            completed_count=len(exchange.outcomes),
            synthetic_count=synthetic_count,
        )

    def on_tool_exchange_result_recorded(
        self, exchange: ToolExchange, outcome: ToolResultRecord
    ) -> None:
        """Persist one safe result outcome after its message is journaled."""
        self._journal.tool_exchange_result_recorded(
            exchange.exchange_id,
            tool_use_id=outcome.tool_use_id,
            status=outcome.status,
            synthetic=outcome.synthetic,
        )

    def on_tool_exchange_aborted(self, exchange: ToolExchange, *, repaired: bool) -> None:
        """Persist interruption/recovery without exposing tool payloads."""
        self._journal.tool_exchange_aborted(
            exchange.exchange_id,
            call_count=len(exchange.call_ids),
            repaired=repaired,
            event_id=(
                tool_exchange_recovery_event_id(exchange.exchange_id, "repaired")
                if repaired
                else tool_exchange_recovery_event_id(exchange.exchange_id, "aborted")
            ),
        )

    @property
    def journal(self) -> ConversationJournal:
        """The underlying durable journal (turn markers, tool records)."""
        return self._journal

    def close(self) -> None:
        """Close the underlying journal file handle."""
        self._journal.close()
