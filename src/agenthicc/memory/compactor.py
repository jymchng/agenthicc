"""Conversation compactor — the manual ``/compact`` command (PRD-119, PRD-135).

*Automatic* compaction lives in lauren-ai's runner (PRD-135): the exact-count
compaction ladder fires proactively each turn (``_maybe_compact`` →
``_summarize_memory``) before the hard pre-send guard would resort to lossy
truncation.  This module provides only the user-invoked ``/compact`` command,
which compresses the **whole** session into a dense summary on demand.

It uses lauren-ai's canonical ``Message`` and transport interfaces.  Long
transcripts are split into bounded chunks and reduced through the same
``transport.complete`` contract, so compaction does not depend on a private
lauren-ai helper that may move between releases.

Public API
----------
compact_memory(
    memory, transport, *, model, conv_store, max_input_tokens,
    max_completion_tokens, request_options,
) -> int
"""

from __future__ import annotations

__all__ = [
    "CompactionOutcome",
    "compact_memory",
    "compact_memory_with_result",
    "local_compaction_fallback",
]

import asyncio
import copy
import inspect
import logging
from dataclasses import dataclass, replace
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory

    from agenthicc.runners.usage_ledger import UsageLedger
    from agenthicc.tui.conversation_store import ConversationStore

log = logging.getLogger(__name__)

_ACK = "Understood. Continuing from the summary."
# Conservative chars→tokens used to size each map-reduce chunk's input.
_SUMMARY_INPUT_CHARS_PER_TOKEN: float = 3.0
_SUMMARY_PROMPT_RESERVE_TOKENS: int = 2_000
_SUMMARY_MAX_COMPLETION_TOKENS: int = 1_024
_SUMMARY_RETRY_MAX_COMPLETION_TOKENS: int = 2_048


@runtime_checkable
class _ReplaceStateMemory(Protocol):
    def replace_state(self, messages: Sequence[object], summary: str | None) -> None: ...


@runtime_checkable
class _JournalResetMemory(Protocol):
    def journal_reset(self) -> None: ...


@runtime_checkable
class _SummaryMemory(Protocol):
    @property
    def summary(self) -> str | None: ...


@dataclass(frozen=True)
class CompactionOutcome:
    """Bounded, content-free result of one compaction attempt."""

    status: Literal[
        "compacted_summary",
        "compacted_local_fallback",
        "skipped_summary_unavailable",
        "blocked_durability_error",
        "blocked_irreducible_context",
    ]
    before_tokens: int
    after_tokens: int
    durable: bool
    error_type: str = ""
    summary_result: Literal[
        "summarized",
        "empty_final",
        "reasoning_only",
        "tool_call_only",
        "unrecognized_content",
        "transport_error",
        "cancelled",
        "not_attempted",
    ] = "not_attempted"
    summary_attempts: int = 0
    summary_error_type: str = ""
    fallback_result: Literal[
        "not_needed",
        "compacted_whole_exchanges",
        "compacted_tool_output",
        "irreducible_latest_exchange",
        "no_safe_boundary",
        "durability_error",
    ] = "not_needed"
    recovery_reason: Literal[
        "latest_exchange_too_large",
        "current_input_exceeds_budget",
        "no_protocol_safe_boundary",
        "summary_not_reducing_context",
        "compaction_commit_failed",
        "exact_count_over_budget",
        "",
    ] = ""

    @property
    def applied(self) -> bool:
        """Whether a replacement memory projection was committed."""
        return self.status in {"compacted_summary", "compacted_local_fallback"}


class CompactionRecoveryRequired(RuntimeError):
    """Compaction could not safely commit or fit; preserve session for resume."""

    def __init__(self, outcome: CompactionOutcome) -> None:
        self.outcome = outcome
        self.failure_category = (
            "compaction_persistence_failed"
            if outcome.status == "blocked_durability_error"
            else "context_limit_unresolved"
        )
        details = [f"status={outcome.status}"]
        if outcome.summary_result != "not_attempted":
            details.append(f"summary={outcome.summary_result}")
            details.append(f"summary_attempts={outcome.summary_attempts}")
        if outcome.fallback_result != "not_needed":
            details.append(f"fallback={outcome.fallback_result}")
        if outcome.recovery_reason:
            details.append(f"reason={outcome.recovery_reason}")
        if outcome.error_type:
            details.append(f"error_type={outcome.error_type}")
        super().__init__("Conversation compaction requires recovery (" + ", ".join(details) + ")")


@dataclass(frozen=True)
class _SummaryResult:
    text: str
    result: Literal[
        "summarized",
        "empty_final",
        "reasoning_only",
        "tool_call_only",
        "unrecognized_content",
        "transport_error",
    ]
    attempts: int
    error_type: str = ""


@dataclass(frozen=True)
class _PreparedLocalFallback:
    outcome: CompactionOutcome
    messages: list[object] | None = None
    summary: str | None = None


async def compact_memory(
    memory: ShortTermMemory,
    transport: object,
    *,
    model: str,
    conv_store: ConversationStore | None = None,
    max_input_tokens: int = 0,
    usage_ledger: UsageLedger | None = None,
    session_id: str = "",
    run_id: str = "",
    max_completion_tokens: int | None = None,
    request_options: object | None = None,
) -> int:
    """Compatibility wrapper: summarize *memory* and return its token estimate.

    Uses lauren-ai's map-reduce summariser so a history larger than the model
    window is compressed via bounded chunks rather than a single over-budget
    call.  ``max_input_tokens`` (0 → one shot) bounds each chunk's input.

    Sets ``conv_store.compaction_active`` for the duration and unconditionally
    clears it (even on error).  Returns the new ``token_estimate``.
    """
    outcome = await compact_memory_with_result(
        memory,
        transport,
        model=model,
        conv_store=conv_store,
        max_input_tokens=max_input_tokens,
        usage_ledger=usage_ledger,
        session_id=session_id,
        run_id=run_id,
        max_completion_tokens=max_completion_tokens,
        request_options=request_options,
    )
    return outcome.after_tokens


async def compact_memory_with_result(
    memory: ShortTermMemory,
    transport: object,
    *,
    model: str,
    conv_store: ConversationStore | None = None,
    max_input_tokens: int = 0,
    usage_ledger: UsageLedger | None = None,
    session_id: str = "",
    run_id: str = "",
    max_completion_tokens: int | None = None,
    request_options: object | None = None,
    emit_result: bool = True,
) -> CompactionOutcome:
    """Summarize into a candidate projection and commit it transactionally.

    Summary/provider failures leave the original projection untouched. A
    journal-backed memory must expose ``replace_state``; mutations are not
    attempted through the older mutate-then-journal hook.
    """
    before_tokens = _memory_tokens(memory)
    if conv_store is not None:
        try:
            conv_store.compaction_active.set(True)
            conv_store.append_event("system", {"text": "⎋ Compacting conversation…"})
            conv_store.append_event(
                "compaction_status",
                {"status": "started", "before_tokens": before_tokens},
            )
        except Exception as exc:  # noqa: BLE001 - presentation is advisory
            log.warning("could not project compaction start (%s)", type(exc).__name__)

    try:
        # Yield so the spinner repaint flushes before the LLM call begins.
        await asyncio.sleep(0)

        messages = list(memory._messages)
        prior_summary = _memory_summary(memory)
        transcript = _format_transcript(messages)
        if isinstance(prior_summary, str) and prior_summary.strip():
            transcript = f"Earlier summary:\n{prior_summary.strip()}\n\n{transcript}"
        had_transcript = bool(transcript.strip())
        max_input_chars = 0
        if max_input_tokens > 0:
            # Size map chunks for the largest completion budget that may be
            # used by the empty-response retry, not just the first attempt.
            output_reserve = _summary_output_limit(max_completion_tokens, retry=True)
            usable = max_input_tokens - output_reserve - _SUMMARY_PROMPT_RESERVE_TOKENS
            max_input_chars = max(2_000, int(usable * _SUMMARY_INPUT_CHARS_PER_TOKEN))
        summary_result = await _summarize_text(
            transport,
            transcript,
            model=model,
            max_input_chars=max_input_chars,
            usage_ledger=usage_ledger,
            session_id=session_id,
            run_id=run_id,
            max_completion_tokens=max_completion_tokens,
            request_options=request_options,
        )
        summary = summary_result.text

        if not summary:
            outcome = CompactionOutcome(
                "skipped_summary_unavailable" if had_transcript else "blocked_irreducible_context",
                before_tokens,
                before_tokens,
                durable=False,
                error_type=summary_result.error_type,
                summary_result=(summary_result.result if had_transcript else "not_attempted"),
                summary_attempts=(summary_result.attempts if had_transcript else 0),
                summary_error_type=(summary_result.error_type if had_transcript else ""),
                fallback_result="no_safe_boundary" if not had_transcript else "not_needed",
                recovery_reason="no_protocol_safe_boundary" if not had_transcript else "",
            )
            if emit_result:
                _emit_outcome(conv_store, outcome)
            return outcome

        candidate = [
            {"role": "user", "content": f"[COMPACT SUMMARY]\n{summary}"},
            {"role": "assistant", "content": _ACK},
        ]
        from lauren_ai._memory import ShortTermMemory as _ShortTermMemory  # noqa: PLC0415

        candidate_memory = _ShortTermMemory(max_tokens=max(1, max_input_tokens))
        candidate_memory._messages = list(candidate)
        candidate_tokens = _memory_tokens(candidate_memory)
        if candidate_tokens >= before_tokens:
            outcome = CompactionOutcome(
                "blocked_irreducible_context",
                before_tokens,
                before_tokens,
                durable=False,
                summary_result="summarized",
                summary_attempts=summary_result.attempts,
                fallback_result="irreducible_latest_exchange",
                recovery_reason="summary_not_reducing_context",
            )
            if emit_result:
                _emit_outcome(conv_store, outcome)
            return outcome
        try:
            # The generated summary incorporates the rolling summary above;
            # clear its separate field to avoid duplicating it in the next
            # provider request.
            _replace_state(memory, candidate, None)
        except Exception as exc:  # noqa: BLE001 - typed blocked outcome, old state retained
            log.warning("compactor: durable replacement failed (%s)", type(exc).__name__)
            outcome = CompactionOutcome(
                "blocked_durability_error",
                before_tokens,
                before_tokens,
                durable=False,
                error_type=type(exc).__name__[:96],
                summary_result="summarized",
                summary_attempts=summary_result.attempts,
                summary_error_type=summary_result.error_type,
                fallback_result="durability_error",
                recovery_reason="compaction_commit_failed",
            )
            if emit_result:
                _emit_outcome(conv_store, outcome)
            return outcome

        after_tokens = _memory_tokens(memory)
        outcome = CompactionOutcome(
            "compacted_summary",
            before_tokens,
            after_tokens,
            durable=True,
            summary_result="summarized",
            summary_attempts=summary_result.attempts,
        )
        log.info("compactor: compacted to ~%d tokens", after_tokens)
        if emit_result:
            _emit_outcome(conv_store, outcome)
        return outcome

    except Exception as exc:  # noqa: BLE001
        log.warning("compactor: summary failed (%s)", type(exc).__name__)
        outcome = CompactionOutcome(
            "skipped_summary_unavailable",
            before_tokens,
            before_tokens,
            durable=False,
            error_type=type(exc).__name__[:96],
            summary_result="transport_error",
            summary_error_type=type(exc).__name__[:96],
        )
        if emit_result:
            _emit_outcome(conv_store, outcome)
        return outcome

    finally:
        if conv_store is not None:
            try:
                conv_store.compaction_active.set(False)
            except Exception as exc:  # noqa: BLE001 - presentation is advisory
                log.warning("could not clear compaction indicator (%s)", type(exc).__name__)


def local_compaction_fallback(
    memory: ShortTermMemory,
    *,
    max_input_tokens: int,
) -> CompactionOutcome:
    """Commit a protocol-preserving local fallback.

    It first removes complete old exchanges. If the latest exchange remains
    oversized, it can excerpt plain-text tool-result payloads while preserving
    the tool call/result structure and all user-authored content. Unknown or
    multimodal payloads are left untouched and can therefore remain blocked.
    """
    prepared = _prepare_local_compaction_fallback(memory, max_input_tokens=max_input_tokens)
    if prepared.messages is None:
        return prepared.outcome
    try:
        _replace_state(memory, prepared.messages, prepared.summary)
    except Exception as exc:  # noqa: BLE001 - durable commit is the publish boundary
        log.warning("compactor: local replacement failed (%s)", type(exc).__name__)
        return replace(
            prepared.outcome,
            status="blocked_durability_error",
            before_tokens=prepared.outcome.before_tokens,
            after_tokens=prepared.outcome.before_tokens,
            durable=False,
            error_type=type(exc).__name__[:96],
            fallback_result="durability_error",
            recovery_reason="compaction_commit_failed",
        )
    return replace(
        prepared.outcome,
        after_tokens=_memory_tokens(memory),
        durable=True,
    )


def _prepare_local_compaction_fallback(
    memory: ShortTermMemory,
    *,
    max_input_tokens: int,
    target_tokens: int | None = None,
) -> _PreparedLocalFallback:
    """Build, but do not publish, a complete-exchange/tool-output candidate."""
    before = _memory_tokens(memory)
    original = copy.deepcopy(list(memory._messages))
    if not original:
        return _PreparedLocalFallback(
            CompactionOutcome(
                "blocked_irreducible_context",
                before,
                before,
                durable=False,
                fallback_result="no_safe_boundary",
                recovery_reason="no_protocol_safe_boundary",
            )
        )

    try:
        target = max(
            1,
            target_tokens if target_tokens is not None else int(max_input_tokens * 0.75),
        )
        non_system = [message for message in original if _field(message, "role", "") != "system"]
        starts = [
            index
            for index, message in enumerate(non_system)
            if _is_conversational_user_message(message)
        ]
        if not starts:
            return _PreparedLocalFallback(
                CompactionOutcome(
                    "blocked_irreducible_context",
                    before,
                    before,
                    durable=False,
                    fallback_result="no_safe_boundary",
                    recovery_reason="no_protocol_safe_boundary",
                )
            )
        system_messages = [
            message for message in original if _field(message, "role", "") == "system"
        ]
        prior_summary = _memory_summary(memory)
        first_kept = starts[0]
        candidate_messages = system_messages + non_system[first_kept:]
        candidate_tokens = _candidate_memory_tokens(
            candidate_messages, prior_summary, max_input_tokens
        )
        remaining_starts = starts[1:]
        pruned = False
        while candidate_tokens > target and remaining_starts:
            first_kept = remaining_starts.pop(0)
            candidate_messages = system_messages + non_system[first_kept:]
            candidate_tokens = _candidate_memory_tokens(
                candidate_messages, prior_summary, max_input_tokens
            )
            pruned = True

        if candidate_tokens <= target and candidate_tokens < before and candidate_messages:
            return _PreparedLocalFallback(
                CompactionOutcome(
                    "compacted_local_fallback",
                    before,
                    candidate_tokens,
                    durable=False,
                    fallback_result="compacted_whole_exchanges",
                ),
                candidate_messages,
                prior_summary,
            )

        # At this point the candidate starts at the newest conversational
        # exchange. Only generated, plain-text tool results may be excerpted.
        reduced = _reduce_tool_output_to_target(
            candidate_messages,
            prior_summary=prior_summary,
            max_input_tokens=max_input_tokens,
            target_tokens=target,
            before_tokens=before,
        )
        if reduced is not None:
            reduced_messages, reduced_tokens = reduced
            return _PreparedLocalFallback(
                CompactionOutcome(
                    "compacted_local_fallback",
                    before,
                    reduced_tokens,
                    durable=False,
                    fallback_result="compacted_tool_output",
                ),
                reduced_messages,
                prior_summary,
            )

        has_reducible_output = _has_reducible_tool_output(candidate_messages)
        return _PreparedLocalFallback(
            CompactionOutcome(
                "blocked_irreducible_context",
                before,
                before,
                durable=False,
                fallback_result="irreducible_latest_exchange",
                recovery_reason=(
                    "latest_exchange_too_large"
                    if has_reducible_output or pruned
                    else "no_protocol_safe_boundary"
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001 - preparation failure does not mutate memory
        log.warning("compactor: local candidate preparation failed (%s)", type(exc).__name__)
        return _PreparedLocalFallback(
            CompactionOutcome(
                "blocked_irreducible_context",
                before,
                before,
                durable=False,
                error_type=type(exc).__name__[:96],
                fallback_result="no_safe_boundary",
                recovery_reason="no_protocol_safe_boundary",
            )
        )


def _candidate_memory_tokens(
    messages: list[object], summary: str | None, max_input_tokens: int
) -> int:
    from lauren_ai._memory import ShortTermMemory as _ShortTermMemory  # noqa: PLC0415

    candidate_memory = _ShortTermMemory(max_tokens=max(1, max_input_tokens))
    candidate_memory._messages = copy.deepcopy(messages)
    candidate_memory._summary = summary
    return _memory_tokens(candidate_memory)


def _has_reducible_tool_output(messages: list[object]) -> bool:
    return any(_tool_result_text_segments(message) for message in messages)


def _reduce_tool_output_to_target(
    messages: list[object],
    *,
    prior_summary: str | None,
    max_input_tokens: int,
    target_tokens: int,
    before_tokens: int,
) -> tuple[list[object], int] | None:
    max_segment_chars = max(
        (len(text) for message in messages for text in _tool_result_text_segments(message)),
        default=0,
    )
    if max_segment_chars <= 0:
        return None

    def candidate_for(retained_chars: int) -> tuple[list[object], int, bool]:
        candidate = copy.deepcopy(messages)
        changed = False
        for message in candidate:
            if not isinstance(message, Mapping) or _field(message, "role", "") != "user":
                continue
            blocks = message.get("content")
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                content = block.get("content")
                clipped, was_changed = _excerpt_tool_result_content(content, retained_chars)
                if was_changed:
                    block["content"] = clipped
                    changed = True
        tokens = _candidate_memory_tokens(candidate, prior_summary, max_input_tokens)
        return candidate, tokens, changed

    best: tuple[list[object], int] | None = None
    low = 0
    high = max_segment_chars
    # Bound candidate allocations even for unexpectedly huge tool output. The
    # zero-retained-content candidate is checked separately below, so the cap
    # cannot turn a safe fallback into a false irreducible result.
    for _ in range(24):
        if low > high:
            break
        retained = (low + high) // 2
        candidate, tokens, changed = candidate_for(retained)
        fits = changed and tokens <= target_tokens and tokens < before_tokens
        if fits:
            best = candidate, tokens
            low = retained + 1
        else:
            high = retained - 1
    if best is None:
        candidate, tokens, changed = candidate_for(0)
        if changed and tokens <= target_tokens and tokens < before_tokens:
            best = candidate, tokens
    return best


def _tool_result_text_segments(message: object) -> list[str]:
    if _field(message, "role", "") != "user":
        return []
    blocks = _field(message, "content", None)
    if not isinstance(blocks, list):
        return []
    segments: list[str] = []
    for block in blocks:
        if not isinstance(block, Mapping) or block.get("type") != "tool_result":
            continue
        content = block.get("content")
        if isinstance(content, str):
            if content:
                segments.append(content)
            continue
        if isinstance(content, list) and all(
            isinstance(part, str)
            or (
                isinstance(part, Mapping)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            )
            for part in content
        ):
            for part in content:
                value = part if isinstance(part, str) else part.get("text", "")
                if isinstance(value, str) and value:
                    segments.append(value)
    return segments


def _excerpt_tool_result_content(content: object, retained_chars: int) -> tuple[object, bool]:
    if isinstance(content, str):
        excerpt = _excerpt_text(content, retained_chars)
        return excerpt, excerpt != content
    if not isinstance(content, list) or not all(
        isinstance(part, str)
        or (
            isinstance(part, Mapping)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
        for part in content
    ):
        return content, False
    updated: list[object] = []
    changed = False
    for part in content:
        if isinstance(part, str):
            excerpt = _excerpt_text(part, retained_chars)
            updated.append(excerpt)
            changed = changed or excerpt != part
        else:
            copied = dict(part)
            original = str(copied.get("text", ""))
            excerpt = _excerpt_text(original, retained_chars)
            copied["text"] = excerpt
            updated.append(copied)
            changed = changed or excerpt != original
    return updated, changed


def _excerpt_text(text: str, retained_chars: int) -> str:
    if len(text) <= retained_chars:
        return text
    retained = max(0, retained_chars)
    head_count = (retained + 1) // 2
    tail_count = retained // 2
    head = text[:head_count] if head_count else ""
    tail = text[-tail_count:] if tail_count else ""
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n[… {omitted} characters omitted …]\n{tail}"


def _replace_state(
    memory: ShortTermMemory, messages: Sequence[object], summary: str | None
) -> None:
    """Publish a prepared replacement through the durable memory contract."""
    if isinstance(memory, _ReplaceStateMemory):
        memory.replace_state(messages, summary)
        return
    # A journal without the transaction hook cannot safely compact: mutating
    # first and attempting a later reset can split the live/durable state.
    if isinstance(memory, _JournalResetMemory):
        raise RuntimeError("journaled memory does not implement transactional replace_state")
    memory._messages = list(messages)
    memory._summary = summary


def _memory_tokens(memory: ShortTermMemory) -> int:
    value = memory.token_estimate
    estimate = max(0, value) if isinstance(value, int) else 0
    summary = _memory_summary(memory)
    if isinstance(summary, str) and summary:
        estimate += max(1, (len(summary) + 2) // 3)
    return estimate


def _memory_summary(memory: ShortTermMemory) -> str | None:
    """Read the optional summary from full memories and lightweight adapters."""
    return memory.summary if isinstance(memory, _SummaryMemory) else None


def _is_conversational_user_message(message: object) -> bool:
    """Distinguish a user turn from a user-role tool-result envelope."""
    if _field(message, "role", "") != "user":
        return False
    content = _field(message, "content", "")
    if isinstance(content, list) and content:
        blocks = [block for block in content if _field(block, "type", "") != "text"]
        if blocks and all(_field(block, "type", "") == "tool_result" for block in blocks):
            return False
    return True


def _emit_outcome(conv_store: ConversationStore | None, outcome: CompactionOutcome) -> None:
    """Project redacted status only; never include conversation contents."""
    if conv_store is None:
        return
    reasons = {
        "latest_exchange_too_large": "latest exchange exceeds the usable context budget",
        "current_input_exceeds_budget": "current input exceeds the usable context budget",
        "no_protocol_safe_boundary": "no protocol-safe history boundary can fit",
        "summary_not_reducing_context": "summary did not reduce the context",
        "compaction_commit_failed": "durable compaction could not be committed",
        "exact_count_over_budget": "candidate still exceeds the exact provider token budget",
        "": "",
    }
    if outcome.status == "compacted_summary":
        label = f"⎋ Compacted → ~{outcome.after_tokens:,} tokens"
    elif outcome.status == "compacted_local_fallback":
        if outcome.fallback_result == "compacted_tool_output":
            label = "⎋ Compaction fallback: excerpted generated tool output; omissions are marked"
        else:
            label = "⎋ Compaction fallback: retained recent complete exchanges"
    elif outcome.status == "skipped_summary_unavailable":
        label = (
            "⎋ Compaction summary unavailable "
            f"({outcome.summary_result}; {outcome.summary_attempts} attempts); conversation unchanged"
        )
    elif outcome.status == "blocked_durability_error":
        label = "⎋ Compaction blocked: durable memory could not be committed"
    else:
        reason = reasons.get(outcome.recovery_reason, "context could not be reduced safely")
        cause = (
            f" after summary {outcome.summary_result}"
            if outcome.summary_result not in {"not_attempted", "summarized"}
            else ""
        )
        label = f"⎋ Compaction blocked: {reason}{cause}"

    if outcome.status == "compacted_local_fallback" and outcome.summary_result not in {
        "not_attempted",
        "summarized",
    }:
        label += f" (summary unavailable: {outcome.summary_result})"
    try:
        conv_store.append_event(
            "compaction_status",
            {
                "status": outcome.status,
                "before_tokens": outcome.before_tokens,
                "after_tokens": outcome.after_tokens,
                "durable": outcome.durable,
                "error_type": outcome.error_type,
                "summary_result": outcome.summary_result,
                "summary_attempts": outcome.summary_attempts,
                "summary_error_type": outcome.summary_error_type,
                "fallback_result": outcome.fallback_result,
                "recovery_reason": outcome.recovery_reason,
            },
        )
        conv_store.append_event("system", {"text": label})
    except Exception as exc:  # noqa: BLE001 - observability must not fail a turn
        log.warning("compaction status projection failed (%s)", type(exc).__name__)


# ── internal helpers ──────────────────────────────────────────────────────────


def _format_transcript(messages: list[object]) -> str:
    """Render a message list as a plain-text transcript for the summariser."""
    lines: list[str] = []
    for msg in messages:
        role: str = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")
        content: object = (
            msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        )

        if role == "system":
            continue  # system prompt is re-injected each turn; skip it

        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    parts.append(str(block))
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    import json  # noqa: PLC0415

                    parts.append(
                        f"[tool_call:{block.get('name', '')}({json.dumps(block.get('input', {}))[:200]})]"
                    )
                elif btype == "tool_result":
                    raw = block.get("content", "")
                    preview = str(raw)[:500]
                    parts.append(f"[tool_result:{preview}{'…' if len(str(raw)) > 500 else ''}]")
                else:
                    parts.append(str(block))
            text = " ".join(p for p in parts if p)
        else:
            text = str(content)

        if text.strip():
            lines.append(f"{role.upper()}: {text.strip()}")

    return "\n\n".join(lines)


async def _summarize_text(
    transport: object,
    transcript: str,
    *,
    model: str,
    max_input_chars: int = 0,
    usage_ledger: UsageLedger | None = None,
    session_id: str = "",
    run_id: str = "",
    max_completion_tokens: int | None = None,
    request_options: object | None = None,
) -> _SummaryResult:
    """Summarise text using lauren-ai's stable transport contract.

    A zero limit performs one completion, preserving the manual compact
    command's normal behaviour.  A positive limit performs map/reduce calls
    with bounded prompts so a large history never becomes one oversized
    request.
    """
    from lauren_ai._transport import Message  # noqa: PLC0415

    prompt_prefix = (
        "Summarise the following conversation portion concisely and factually. "
        "Preserve decisions, constraints, paths, and unfinished work.\n\n"
    )
    chunks = [transcript]
    if max_input_chars > 0 and len(transcript) > max_input_chars:
        chunks = [
            transcript[index : index + max_input_chars]
            for index in range(0, len(transcript), max_input_chars)
        ]

    attempts = 0

    async def summarize_chunk_call(chunk: str, *, retry: bool) -> _SummaryResult:
        """Make one bounded call and classify its final output without content."""
        nonlocal attempts
        attempts += 1
        usage_call = None
        if usage_ledger is not None:
            usage_call = usage_ledger.begin_call(
                run_id=run_id or f"compaction:{session_id or 'session'}",
                model=model,
                category="compaction",
            )
        output_limit = _summary_output_limit(
            max_completion_tokens,
            retry=retry,
        )
        prompt = prompt_prefix + chunk
        if retry:
            prompt = (
                "The previous summarisation response contained no final text. "
                "Return a non-empty summary in the final response now. Do not emit "
                "reasoning only, do not call tools, and output only the concise "
                "conversation summary.\n\n" + prompt
            )
        try:
            kwargs: dict[str, object] = {
                "model": model,
                "system": (
                    "You are a conversation summariser. Be concise and factual. "
                    "Return the summary as final answer text; never leave the final "
                    "answer empty."
                ),
                # Keep the legacy argument populated for older transports.  New
                # OpenAI-compatible transports use max_completion_tokens below,
                # which includes reasoning-token budgets where supported.
                "max_tokens": output_limit,
                "temperature": 0.0,
                "stream": False,
            }
            complete = getattr(transport, "complete")
            if _accepts_keyword(complete, "max_completion_tokens"):
                kwargs["max_completion_tokens"] = output_limit
            if request_options is not None and _accepts_keyword(complete, "request_options"):
                kwargs["request_options"] = _resolve_request_options(request_options)
            result = await complete(
                [Message.user(prompt)],
                **{**kwargs, "stream": retry},
            )
            text, result_usage, response_kind = await _completion_text_async(result)
            if usage_ledger is not None and usage_call is not None:
                usage_ledger.complete(
                    usage_call,
                    result_usage,
                    cost_usd=_provider_cost(result_usage, model),
                )
            return _SummaryResult(
                text=text,
                result="summarized" if text else response_kind,
                attempts=attempts,
            )
        except asyncio.CancelledError:
            if usage_ledger is not None and usage_call is not None:
                usage_ledger.complete(
                    usage_call,
                    source="unknown",
                    lifecycle="failed",
                )
            raise
        except Exception as exc:  # noqa: BLE001 - classification; no transcript in result
            if usage_ledger is not None and usage_call is not None:
                usage_ledger.complete(
                    usage_call,
                    source="unknown",
                    lifecycle="failed",
                )
            return _SummaryResult(
                text="",
                result="transport_error",
                attempts=attempts,
                error_type=type(exc).__name__[:96],
            )

    async def summarize_chunk(chunk: str) -> _SummaryResult:
        """Summarise once, then retry an empty final response once."""
        summary = await summarize_chunk_call(chunk, retry=False)
        if summary.text or summary.result == "transport_error":
            return summary
        return await summarize_chunk_call(chunk, retry=True)

    partials: list[str] = []
    last_result: _SummaryResult | None = None
    for chunk in chunks:
        chunk_result = await summarize_chunk(chunk)
        last_result = chunk_result
        if not chunk_result.text:
            return _SummaryResult(
                text="",
                result=chunk_result.result,
                attempts=attempts,
                error_type=chunk_result.error_type,
            )
        partials.append(chunk_result.text)

    while len(partials) > 1:
        reduced = "\n\n".join(partials)
        if max_input_chars <= 0 or len(reduced) <= max_input_chars:
            reduced_result = await summarize_chunk(reduced)
            return replace(reduced_result, attempts=attempts)
        chunks = [
            reduced[index : index + max_input_chars]
            for index in range(0, len(reduced), max_input_chars)
        ]
        partials = []
        for chunk in chunks:
            chunk_result = await summarize_chunk(chunk)
            last_result = chunk_result
            if not chunk_result.text:
                return _SummaryResult(
                    text="",
                    result=chunk_result.result,
                    attempts=attempts,
                    error_type=chunk_result.error_type,
                )
            partials.append(chunk_result.text)
    if partials:
        return _SummaryResult(partials[0], "summarized", attempts)
    return last_result or _SummaryResult("", "empty_final", attempts)


def _summary_output_limit(configured: int | None, *, retry: bool) -> int:
    """Choose a useful completion budget without inheriting an unbounded cap."""
    floor = _SUMMARY_RETRY_MAX_COMPLETION_TOKENS if retry else _SUMMARY_MAX_COMPLETION_TOKENS
    if not isinstance(configured, int) or configured <= 0:
        return floor
    # A summary does not need the full turn budget.  Cap an unusually large
    # session setting so a provider cannot spend an excessive reasoning budget
    # on a bounded maintenance operation.
    return min(max(floor, configured), 4_096)


def _accepts_keyword(callable_obj: object, name: str) -> bool:
    """Return whether a transport method can receive an optional keyword."""
    if not callable(callable_obj):
        return False
    try:
        parameters = inspect.signature(callable_obj).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == name or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _resolve_request_options(value: object) -> object:
    """Resolve config-shaped request options without coupling transports to config."""
    try:
        from agenthicc.config import RequestOptionSettings  # noqa: PLC0415

        if isinstance(value, RequestOptionSettings):
            return value.resolve()
    except (ImportError, ValueError):
        pass
    return value


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _completion_text(result: object) -> str:
    """Extract final answer text across canonical and provider-shaped results.

    The canonical lauren-ai ``Completion`` intentionally keeps reasoning
    blocks separate from ``content``.  Reasoning-only output is not a valid
    conversation summary, so it is never used as a substitute.  Raw provider
    content is considered only when the transport explicitly retained it.
    """
    candidates: list[object] = [
        _field(result, "content"),
        _field(result, "output_text"),
    ]
    raw = _field(result, "raw_response")
    if raw is not None:
        candidates.append(_field(raw, "output_text"))
        choices = _field(raw, "choices", [])
        if isinstance(choices, (list, tuple)) and choices:
            message = _field(choices[0], "message")
            if message is not None:
                candidates.append(_field(message, "content"))
            candidates.append(_field(choices[0], "text"))
        candidates.append(_field(raw, "content"))
    for candidate in candidates:
        text = _content_text(candidate)
        if text:
            return text
    return ""


async def _completion_text_async(
    result: object,
) -> tuple[
    str,
    object | None,
    Literal[
        "empty_final", "reasoning_only", "tool_call_only", "unrecognized_content", "summarized"
    ],
]:
    """Extract text and usage from either a completion or stream.

    Some OpenAI-compatible reasoning endpoints return an empty canonical
    ``content`` field when ``stream=False`` even though the streamed response
    contains the final answer after the reasoning deltas.  Compaction uses a
    streamed retry for that case.  Only ``delta`` is accumulated here;
    ``thinking_delta`` and tool-call deltas are intentionally not summary
    prose.
    """
    if not hasattr(result, "__aiter__"):
        text = _completion_text(result)
        completion_kind = "summarized" if text else _empty_completion_kind(result)
        return text, _field(result, "usage"), completion_kind

    parts: list[str] = []
    usage: object | None = None
    override: str | None = None
    reasoning_seen = False
    tool_call_seen = False
    async for chunk in result:
        candidate_override = _field(chunk, "guardrail_override")
        if isinstance(candidate_override, str):
            override = candidate_override
        delta = _field(chunk, "delta", "")
        if isinstance(delta, str) and delta:
            parts.append(delta)
        reasoning_seen = reasoning_seen or bool(
            _field(chunk, "thinking_delta")
            or _field(chunk, "reasoning_content_delta")
            or _field(chunk, "thinking_signature")
            or _field(chunk, "redacted_thinking_data")
        )
        tool_call_seen = tool_call_seen or _field(chunk, "tool_call_delta") is not None
        candidate_usage = _field(chunk, "usage")
        if candidate_usage is not None:
            usage = candidate_usage

    if override is not None:
        text = _content_text(override)
    else:
        text = _content_text("".join(parts))
    if text:
        return text, usage, "summarized"
    if reasoning_seen and not tool_call_seen:
        stream_kind: Literal[
            "empty_final", "reasoning_only", "tool_call_only", "unrecognized_content", "summarized"
        ] = "reasoning_only"
    elif tool_call_seen and not reasoning_seen:
        stream_kind = "tool_call_only"
    elif reasoning_seen or tool_call_seen:
        stream_kind = "unrecognized_content"
    else:
        stream_kind = "empty_final"
    return "", usage, stream_kind


def _empty_completion_kind(
    result: object,
) -> Literal["empty_final", "reasoning_only", "tool_call_only", "unrecognized_content"]:
    """Classify a no-text response from structure only, never from its content."""
    flags = {"reasoning": False, "tool": False, "unknown": False}

    def inspect_value(value: object, *, depth: int = 0) -> None:
        if depth > 8 or value is None or isinstance(value, (str, int, float, bool)):
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                inspect_value(item, depth=depth + 1)
            return
        if isinstance(value, Mapping):
            block_type = str(value.get("type", "")).casefold()
            if block_type in {"thinking", "redacted_thinking", "reasoning", "reasoning_content"}:
                flags["reasoning"] = True
                return
            if block_type in {"tool_use", "tool_call", "tool_calls", "tool_result"}:
                flags["tool"] = True
                return
            if block_type == "text":
                return
            recognized = False
            for key in ("content", "output", "blocks", "choices", "message"):
                if key in value:
                    recognized = True
                    inspect_value(value[key], depth=depth + 1)
            for key in ("thinking_blocks", "reasoning_content", "reasoning", "thinking"):
                child = value.get(key)
                if child:
                    flags["reasoning"] = True
                    recognized = True
            for key in ("tool_calls", "tool_call"):
                child = value.get(key)
                if child:
                    flags["tool"] = True
                    recognized = True
            if not recognized and value:
                flags["unknown"] = True
            return

        block_type = str(getattr(value, "type", "")).casefold()
        if block_type in {"thinking", "redacted_thinking", "reasoning", "reasoning_content"}:
            flags["reasoning"] = True
            return
        if block_type in {"tool_use", "tool_call", "tool_calls", "tool_result"}:
            flags["tool"] = True
            return
        inspected = False
        for key in ("content", "output", "blocks", "choices", "message"):
            child = getattr(value, key, None)
            if child is not None:
                inspected = True
                inspect_value(child, depth=depth + 1)
        for key in ("thinking_blocks", "reasoning_content", "reasoning", "thinking"):
            child = getattr(value, key, None)
            if child:
                flags["reasoning"] = True
                inspected = True
        for key in ("tool_calls", "tool_call"):
            child = getattr(value, key, None)
            if child:
                flags["tool"] = True
                inspected = True
        if not inspected:
            flags["unknown"] = True

    inspect_value(result)
    if flags["reasoning"] and not flags["tool"] and not flags["unknown"]:
        return "reasoning_only"
    if flags["tool"] and not flags["reasoning"] and not flags["unknown"]:
        return "tool_call_only"
    if flags["reasoning"] or flags["tool"] or flags["unknown"]:
        return "unrecognized_content"
    return "empty_final"


def _content_text(value: object) -> str:
    """Flatten text blocks without treating reasoning/tool metadata as prose."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        parts = [_content_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, Mapping):
        block_type = str(value.get("type", ""))
        if block_type in {"thinking", "redacted_thinking", "tool_use", "tool_result"}:
            return ""
        for key in ("text", "output_text", "content"):
            if key in value:
                return _content_text(value[key])
        return ""
    if value is None:
        return ""
    block_type = str(getattr(value, "type", ""))
    if block_type in {"thinking", "redacted_thinking", "tool_use", "tool_result"}:
        return ""
    for key in ("text", "output_text", "content"):
        candidate = getattr(value, key, None)
        if candidate is not None:
            return _content_text(candidate)
    return ""


def _provider_cost(usage: object | None, model: str) -> float | None:
    """Return a provider-library estimate without coupling to a transport."""
    method = getattr(usage, "cost_usd", None)
    if not callable(method):
        return None
    try:
        value = float(method(model))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None
