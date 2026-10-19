"""Deterministic recovery for a session's interrupted tool-result tail.

Lauren-ai deliberately rejects ``non_adjacent_results`` because a generic
memory implementation cannot know whether a result stranded behind a later
message belongs to the earlier assistant batch. Agenthicc can make that
decision at its session boundary: the persisted tool IDs identify the exact
assistant batch, and a queued continuation is the only message that may have
been inserted between the call and its late result.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from lauren_ai._memory import ShortTermMemory, ToolHistoryReport

__all__ = [
    "ToolHistoryRecoveryRequired",
    "ToolHistoryRecoveryState",
    "repair_non_adjacent_tool_history",
]


@dataclass(frozen=True, slots=True)
class ToolHistoryRecoveryState:
    """Safe summary of whether persisted conversation history can reach a provider."""

    status: Literal["ready", "repaired", "repair_required", "irrecoverable", "journal_corrupt"] = (
        "ready"
    )
    issue_codes: tuple[str, ...] = ()
    repair_attempted: bool = False

    @property
    def provider_ready(self) -> bool:
        """Whether the current memory passed Lauren-ai's full validator."""
        return self.status in {"ready", "repaired"}


class ToolHistoryRecoveryRequired(RuntimeError):
    """Typed, redacted failure raised before invalid history reaches a provider."""

    def __init__(self, state: ToolHistoryRecoveryState) -> None:
        self.state = state
        codes = ", ".join(state.issue_codes) or "validation_unavailable"
        super().__init__(
            "Conversation tool history is preserved but not provider-ready "
            f"(issues: {codes}); run /history repair before resuming."
        )


def _integrity_error(report: ToolHistoryReport) -> Exception:
    """Build Lauren-ai's typed integrity error from its redacted report."""
    from lauren_ai import ToolConversationIntegrityError  # noqa: PLC0415

    issue = report.first_issue
    if issue is None:
        return ToolHistoryRecoveryRequired(
            ToolHistoryRecoveryState("repair_required", ("validation_failed",), True)
        )
    code = issue.code[:64]
    expected = issue.expected_count
    observed = issue.observed_count
    assistant_index = issue.assistant_index
    repairable = issue.repairable
    return ToolConversationIntegrityError(
        f"Tool conversation repair candidate remains invalid (code={code}, "
        f"expected={expected}, observed={observed})",
        code=code,
        expected_count=expected,
        observed_count=observed,
        assistant_index=assistant_index,
        repairable=repairable,
    )


def _result_id(block: Mapping[str, object]) -> str:
    value = block.get("tool_use_id") or block.get("tool_call_id") or block.get("id")
    return value if isinstance(value, str) else ""


def _matching_result_blocks(
    message: object,
    wanted: frozenset[str],
) -> tuple[list[dict[str, object]], object | None, bool]:
    """Return matching result blocks and a cleaned copy of *message*."""
    if not isinstance(message, Mapping):
        return [], message, False

    message_type = message.get("type")
    if message_type == "tool_result":
        block = dict(message)
        if _result_id(block) in wanted:
            return [block], None, True
        return [], message, False

    role = message.get("role")
    if role == "tool":
        block = {
            "type": "tool_result",
            "tool_use_id": message.get("tool_call_id") or message.get("tool_use_id") or "",
            "content": message.get("content", ""),
        }
        if "is_error" in message:
            block["is_error"] = message["is_error"]
        if _result_id(block) in wanted:
            return [block], None, True
        return [], message, False

    content = message.get("content")
    if not isinstance(content, (list, tuple)):
        return [], message, False

    matches: list[dict[str, object]] = []
    remaining: list[object] = []
    for block in content:
        if isinstance(block, Mapping) and block.get("type") == "tool_result":
            candidate = dict(block)
            if _result_id(candidate) in wanted:
                matches.append(candidate)
                continue
        remaining.append(block)
    if not matches:
        return [], message, False
    cleaned = dict(message)
    cleaned["content"] = remaining
    return matches, cleaned if remaining else None, True


def repair_non_adjacent_tool_history(
    memory: ShortTermMemory,
    *,
    commit: bool = True,
    allowed_missing_result_hashes: frozenset[str] | None = None,
) -> bool:
    """Move late results next to their assistant calls and persist the repair.

    Only ``non_adjacent_results`` is repaired. Unknown, duplicate, orphan, and
    malformed result IDs remain fail-closed. Missing expected results receive
    an explicit interruption result only when their hashed IDs are present in
    ``allowed_missing_result_hashes``; the journaled caller may derive this
    evidence from either an unfinished exchange receipt or a unique missing
    call in the persisted validator report. The synthetic result always says
    the tool outcome is unavailable; it never claims success. ``None`` retains
    the in-memory recovery behavior for a currently owned exchange. With
    ``commit=False``, apply the candidate only to the supplied detached
    projection so journaled callers can combine repairs in one atomic commit.
    """
    try:
        report = memory.validate_tool_history()
    except Exception:
        return False
    issues = [issue for issue in report.issues if issue.code == "non_adjacent_results"]
    if not issues:
        return False

    original_messages = memory._messages
    if not isinstance(original_messages, list):
        return False
    repaired = copy.deepcopy(original_messages)
    changed = False

    # Process later assistant batches first so edits after an earlier batch do
    # not invalidate its original index.
    for issue in sorted(
        issues,
        key=lambda item: int(getattr(item, "assistant_index", -1) or -1),
        reverse=True,
    ):
        assistant_index = issue.assistant_index
        expected_ids = issue.expected_ids
        if not isinstance(assistant_index, int) or not expected_ids:
            continue
        if assistant_index < 0 or assistant_index >= len(repaired):
            continue

        wanted = frozenset(expected_ids)
        found: dict[str, dict[str, object]] = {}
        immediate_cleaned: object | None = None
        immediate_had_match = False

        for index in range(len(repaired) - 1, assistant_index, -1):
            matches, cleaned, removed = _matching_result_blocks(repaired[index], wanted)
            if not removed:
                continue
            for block in matches:
                result_id = _result_id(block)
                if not result_id:
                    continue
                if result_id in found:
                    # A duplicated late result is not a recoverable ordering
                    # race. Leave the original projection untouched and let
                    # lauren-ai report the precise duplicate/orphan issue.
                    return False
                found[result_id] = block
            if index == assistant_index + 1:
                immediate_had_match = True
                immediate_cleaned = cleaned
            elif cleaned is None:
                del repaired[index]
            else:
                repaired[index] = cleaned

        blocks: list[dict[str, object]] = []
        for tool_use_id in expected_ids:
            existing_block = found.get(tool_use_id)
            if existing_block is None:
                if allowed_missing_result_hashes is not None:
                    call_hash = hashlib.sha256(tool_use_id.encode("utf-8")).hexdigest()[:12]
                    if call_hash not in allowed_missing_result_hashes:
                        return False
                existing_block = {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": (
                        "[Tool execution was interrupted before an adjacent result was available.]"
                    ),
                    "is_error": True,
                }
            blocks.append(existing_block)

        if immediate_had_match:
            if immediate_cleaned is None:
                del repaired[assistant_index + 1]
            else:
                repaired[assistant_index + 1] = immediate_cleaned
        repaired.insert(assistant_index + 1, {"role": "user", "content": blocks})
        changed = True

    if not changed:
        return False

    # Validate a detached candidate first.  Calling ``restore`` on journaled
    # memory persists before validation and then needs a second journal reset
    # to roll back a bad candidate, making a failed repair observable as a
    # state mutation.  The validator is a pure projection check, so a shallow
    # memory copy with a deep-copied message list is sufficient and avoids
    # copying journal handles, locks, or tool executors.
    candidate_memory = copy.copy(memory)
    candidate_memory._messages = repaired
    if not commit:
        # The journaled caller may have an independent, repairable missing
        # result elsewhere in the same projection. Defer full validation until
        # it has applied Lauren-ai's canonical missing-result repair to this
        # same detached candidate.
        memory._messages = repaired
        return True

    final_report = candidate_memory.validate_tool_history()
    if not final_report.ok:
        raise _integrity_error(final_report)

    summary = memory._summary
    from agenthicc.memory.journaled import JournaledShortTermMemory  # noqa: PLC0415

    if isinstance(memory, JournaledShortTermMemory):
        memory.commit_repaired_tool_history(repaired, summary)
    else:
        memory.restore({"messages": repaired, "summary": summary})
    return True
