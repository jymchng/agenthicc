"""Conversation compactor — the manual ``/compact`` command (PRD-119, PRD-135).

*Automatic* compaction lives in lauren-ai's runner (PRD-135): the exact-count
compaction ladder fires proactively each turn (``_maybe_compact`` →
``_summarize_memory``) before the hard pre-send guard would resort to lossy
truncation.  This module provides only the user-invoked ``/compact`` command,
which compresses the **whole** session into a dense summary on demand.

It shares lauren-ai's overflow-proof **map-reduce** summariser
(``_summarize_text``) so compacting a history larger than the model window never
itself overflows — the transcript is chunked, each chunk summarised, and the
partials reduced.

Public API
----------
compact_memory(memory, transport, *, model, conv_store, max_input_tokens) -> int
"""

from __future__ import annotations

__all__ = ["compact_memory"]

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory

    from agenthicc.tui.conversation_store import ConversationStore

log = logging.getLogger(__name__)

_ACK = "Understood. Continuing from the summary."
# Conservative chars→tokens used to size each map-reduce chunk's input.
_SUMMARY_INPUT_CHARS_PER_TOKEN: float = 3.0
_SUMMARY_OUTPUT_RESERVE_TOKENS: int = 1_024
_SUMMARY_PROMPT_RESERVE_TOKENS: int = 2_000


async def compact_memory(
    memory: ShortTermMemory,
    transport: object,
    *,
    model: str,
    conv_store: ConversationStore | None = None,
    max_input_tokens: int = 0,
) -> int:
    """Summarise *memory* in-place into a ``[COMPACT SUMMARY]`` / ack pair.

    Uses lauren-ai's map-reduce summariser so a history larger than the model
    window is compressed via bounded chunks rather than a single over-budget
    call.  ``max_input_tokens`` (0 → one shot) bounds each chunk's input.

    Sets ``conv_store.compaction_active`` for the duration and unconditionally
    clears it (even on error).  Returns the new ``token_estimate``.
    """
    from lauren_ai._agents._runner import _summarize_text  # noqa: PLC0415

    if conv_store is not None:
        conv_store.compaction_active.set(True)
        conv_store.append_event("system", {"text": "⎋ Compacting conversation…"})

    try:
        import asyncio  # noqa: PLC0415

        # Yield so the spinner repaint flushes before the LLM call begins.
        await asyncio.sleep(0)

        transcript = _format_transcript(memory._messages)
        max_input_chars = 0
        if max_input_tokens > 0:
            usable = max_input_tokens - _SUMMARY_OUTPUT_RESERVE_TOKENS - _SUMMARY_PROMPT_RESERVE_TOKENS
            max_input_chars = max(2_000, int(usable * _SUMMARY_INPUT_CHARS_PER_TOKEN))
        summary = await _summarize_text(transport, transcript, model=model, max_input_chars=max_input_chars)

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
            text = f"⎋ Compacted → ~{new_estimate:,} tokens" if summary else "⎋ Nothing to compact"
            conv_store.append_event("system", {"text": text})
        return new_estimate

    except Exception as exc:  # noqa: BLE001
        log.warning("compactor: compaction failed: %s", exc)
        if conv_store is not None:
            conv_store.append_event("system", {"text": f"⎋ Compaction failed: {type(exc).__name__}"})
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
        content: object = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")

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
                        f"[tool_call:{block.get('name','')}({json.dumps(block.get('input',{}))[:200]})]"
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
