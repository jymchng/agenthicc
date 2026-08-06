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

__all__ = ["compact_memory"]

import inspect
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

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
    """Summarise *memory* in-place into a ``[COMPACT SUMMARY]`` / ack pair.

    Uses lauren-ai's map-reduce summariser so a history larger than the model
    window is compressed via bounded chunks rather than a single over-budget
    call.  ``max_input_tokens`` (0 → one shot) bounds each chunk's input.

    Sets ``conv_store.compaction_active`` for the duration and unconditionally
    clears it (even on error).  Returns the new ``token_estimate``.
    """
    if conv_store is not None:
        conv_store.compaction_active.set(True)
        conv_store.append_event("system", {"text": "⎋ Compacting conversation…"})

    try:
        import asyncio  # noqa: PLC0415

        # Yield so the spinner repaint flushes before the LLM call begins.
        await asyncio.sleep(0)

        transcript = _format_transcript(memory._messages)
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

        if summary:
            memory._messages = [
                {"role": "user", "content": f"[COMPACT SUMMARY]\n{summary}"},
                {"role": "assistant", "content": _ACK},
            ]
            # PRD-129 Phase 2: replacing _messages in place bypasses the
            # JournaledShortTermMemory append/restore overrides — record the
            # reset so the durable journal stays in sync with the live buffer.
            journal_reset = getattr(memory, "journal_reset", None)
            if callable(journal_reset):
                journal_reset()

        new_estimate = memory.token_estimate
        log.info("compactor: compacted to ~%d tokens", new_estimate)

        if conv_store is not None:
            if summary:
                text = f"⎋ Compacted → ~{new_estimate:,} tokens"
            elif had_transcript:
                # A non-empty history with an empty model response is a
                # failed compaction, not an empty conversation.  Reporting it
                # accurately prevents the operator from mistaking a provider
                # failure for a successful no-op before an overflow retry.
                text = "⎋ Compaction returned an empty summary"
            else:
                text = "⎋ Nothing to compact"
            conv_store.append_event("system", {"text": text})
        return new_estimate

    except Exception as exc:  # noqa: BLE001
        log.warning("compactor: compaction failed: %s", exc)
        if conv_store is not None:
            conv_store.append_event(
                "system", {"text": f"⎋ Compaction failed: {type(exc).__name__}"}
            )
        return memory.token_estimate

    finally:
        if conv_store is not None:
            conv_store.compaction_active.set(False)


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
