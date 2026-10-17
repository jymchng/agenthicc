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

import inspect
import logging
from dataclasses import dataclass
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
        super().__init__(
            "Conversation compaction requires recovery "
            f"(status={outcome.status}, error_type={outcome.error_type or 'none'})"
        )


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
        import asyncio  # noqa: PLC0415

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
        summary = await _summarize_text(
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

        if not summary:
            outcome = CompactionOutcome(
                "skipped_summary_unavailable" if had_transcript else "blocked_irreducible_context",
                before_tokens,
                before_tokens,
                durable=False,
            )
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
            )
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
            )
            _emit_outcome(conv_store, outcome)
            return outcome

        after_tokens = _memory_tokens(memory)
        outcome = CompactionOutcome("compacted_summary", before_tokens, after_tokens, durable=True)
        log.info("compactor: compacted to ~%d tokens", after_tokens)
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
        )
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
    """Commit a bounded recent-message projection without splitting exchanges.

    The fallback only removes complete oldest turns. An individually
    oversized remaining exchange is reported as irreducible rather than
    truncating a tool call/result or user message in place.
    """
    before = _memory_tokens(memory)
    messages = memory._messages
    if not messages:
        return CompactionOutcome("blocked_irreducible_context", before, before, False)

    try:
        from lauren_ai._memory import ShortTermMemory as _ShortTermMemory  # noqa: PLC0415

        target = max(1, int(max_input_tokens * 0.75))
        non_system = [message for message in messages if _field(message, "role", "") != "system"]
        starts = [
            index
            for index, message in enumerate(non_system)
            if _is_conversational_user_message(message)
        ]
        if not starts:
            return CompactionOutcome("blocked_irreducible_context", before, before, durable=False)
        system_messages = [
            message for message in messages if _field(message, "role", "") == "system"
        ]
        first_kept = starts[0]
        candidate_messages = system_messages + non_system[first_kept:]
        candidate_memory = _ShortTermMemory(max_tokens=max(1, max_input_tokens))
        candidate_memory._messages = list(candidate_messages)
        prior_summary = _memory_summary(memory)
        candidate_memory._summary = prior_summary
        after = _memory_tokens(candidate_memory)
        remaining_starts = starts[1:]
        while after > target and remaining_starts:
            first_kept = remaining_starts.pop(0)
            candidate_messages = system_messages + non_system[first_kept:]
            candidate_memory._messages = list(candidate_messages)
            after = _memory_tokens(candidate_memory)
        if after > target or after >= before or not candidate_messages:
            return CompactionOutcome("blocked_irreducible_context", before, before, durable=False)
        _replace_state(memory, candidate_messages, prior_summary)
        committed_after = _memory_tokens(memory)
        return CompactionOutcome("compacted_local_fallback", before, committed_after, durable=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("compactor: local replacement failed (%s)", type(exc).__name__)
        return CompactionOutcome(
            "blocked_durability_error",
            before,
            before,
            durable=False,
            error_type=type(exc).__name__[:96],
        )


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
    labels = {
        "compacted_summary": f"⎋ Compacted → ~{outcome.after_tokens:,} tokens",
        "compacted_local_fallback": "⎋ Compaction fallback: retained recent complete exchanges",
        "skipped_summary_unavailable": "⎋ Compaction returned an empty summary",
        "blocked_durability_error": "⎋ Compaction blocked: durable memory could not be committed",
        "blocked_irreducible_context": "⎋ Compaction blocked: no complete recent exchange fits",
    }
    try:
        conv_store.append_event(
            "compaction_status",
            {
                "status": outcome.status,
                "before_tokens": outcome.before_tokens,
                "after_tokens": outcome.after_tokens,
                "durable": outcome.durable,
                "error_type": outcome.error_type,
            },
        )
        conv_store.append_event("system", {"text": labels[outcome.status]})
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
) -> str:
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

    async def summarize_chunk_call(chunk: str, *, retry: bool) -> str:
        """Make one bounded summary call and return only final answer text."""
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
            text, result_usage = await _completion_text_async(result)
            if usage_ledger is not None and usage_call is not None:
                usage_ledger.complete(
                    usage_call,
                    result_usage,
                    cost_usd=_provider_cost(result_usage, model),
                )
            return text
        except BaseException:
            if usage_ledger is not None and usage_call is not None:
                usage_ledger.complete(
                    usage_call,
                    source="unknown",
                    lifecycle="failed",
                )
            raise

    async def summarize_chunk(chunk: str) -> str:
        """Summarise once, then retry an empty final response once."""
        summary = await summarize_chunk_call(chunk, retry=False)
        if summary:
            return summary
        return await summarize_chunk_call(chunk, retry=True)

    partials = [await summarize_chunk(chunk) for chunk in chunks]
    while len(partials) > 1:
        reduced = "\n\n".join(partials)
        if max_input_chars <= 0 or len(reduced) <= max_input_chars:
            return await summarize_chunk(reduced)
        chunks = [
            reduced[index : index + max_input_chars]
            for index in range(0, len(reduced), max_input_chars)
        ]
        partials = [await summarize_chunk(chunk) for chunk in chunks]
    return partials[0] if partials else ""


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


async def _completion_text_async(result: object) -> tuple[str, object | None]:
    """Extract text and usage from either a completion or stream.

    Some OpenAI-compatible reasoning endpoints return an empty canonical
    ``content`` field when ``stream=False`` even though the streamed response
    contains the final answer after the reasoning deltas.  Compaction uses a
    streamed retry for that case.  Only ``delta`` is accumulated here;
    ``thinking_delta`` and tool-call deltas are intentionally not summary
    prose.
    """
    if not hasattr(result, "__aiter__"):
        return _completion_text(result), _field(result, "usage")

    parts: list[str] = []
    usage: object | None = None
    override: str | None = None
    async for chunk in result:
        candidate_override = _field(chunk, "guardrail_override")
        if isinstance(candidate_override, str):
            override = candidate_override
        delta = _field(chunk, "delta", "")
        if isinstance(delta, str) and delta:
            parts.append(delta)
        candidate_usage = _field(chunk, "usage")
        if candidate_usage is not None:
            usage = candidate_usage

    if override is not None:
        return _content_text(override), usage
    return _content_text("".join(parts)), usage


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
