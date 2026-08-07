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
from collections.abc import Mapping

__all__ = ["repair_non_adjacent_tool_history"]


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


def repair_non_adjacent_tool_history(memory: object) -> bool:
    """Move late results next to their assistant calls and persist the repair.

    Only ``non_adjacent_results`` is repaired. Unknown, duplicate, orphan, and
    malformed result IDs remain fail-closed. Missing expected results receive
    an explicit interruption result, matching lauren-ai's normal missing-tail
    recovery contract.
    """
    validate = getattr(memory, "validate_tool_history", None)
    if not callable(validate):
        return False
    try:
        report = validate()
    except Exception:
        return False
    issues = [
        issue
        for issue in getattr(report, "issues", ())
        if getattr(issue, "code", "") == "non_adjacent_results"
    ]
    if not issues:
        return False

    original_messages = getattr(memory, "_messages", None)
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
        assistant_index = getattr(issue, "assistant_index", None)
        expected_ids = tuple(
            value for value in getattr(issue, "expected_ids", ()) if isinstance(value, str)
        )
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

    snapshot = getattr(memory, "snapshot", None)
    original_snapshot = snapshot() if callable(snapshot) else copy.deepcopy(original_messages)
    restore = getattr(memory, "restore", None)
    try:
        if callable(restore):
            summary = (
                original_snapshot.get("summary") if isinstance(original_snapshot, Mapping) else None
            )
            # Clear the stale exchange so a late task cannot append an orphan
            # result after the queued continuation.
            restore({"messages": repaired, "summary": summary})
        else:
            memory._messages = repaired  # type: ignore[attr-defined]
        final_report = validate()
        if not getattr(final_report, "ok", False):
            raise ValueError("repaired tool history remains invalid")
    except Exception:
        if callable(restore):
            restore(original_snapshot)
        else:
            memory._messages = original_messages  # type: ignore[attr-defined]
        raise
    return True
