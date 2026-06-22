"""Conversation compactor (PRD-119).

Summarises an oversized ShortTermMemory into a compact two-message history so
that subsequent LLM calls stay within the model's context-window limit.

Public API
----------
should_compact(memory, exec_cfg) -> bool
compact_memory(memory, transport, *, model, conv_store) -> int
"""

from __future__ import annotations

__all__ = ["should_compact", "compact_memory"]

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.config import ExecutionSettings
    from agenthicc.tui.conversation_store import ConversationStore

log = logging.getLogger(__name__)

_COMPACT_SYSTEM = (
    "You are a conversation summariser. "
    "Compress the following conversation history into a single concise summary "
    "that preserves all key facts, decisions, file paths, code changes, tool "
    "results, and outstanding tasks. The summary will replace the original "
    "history in the agent's memory — nothing omitted from the summary can be "
    "recovered. Be dense and precise."
)

_ACK = "Understood. Continuing from the summary."


def should_compact(
    memory: ShortTermMemory,
    exec_cfg: ExecutionSettings | None,
) -> bool:
    """Return True when auto-compact should fire before the next API call."""
    if exec_cfg is None:
        return False
    if not getattr(exec_cfg, "auto_compact", True):
        return False
    threshold: int = getattr(exec_cfg, "compact_threshold_tokens", 1_000_000)
    return memory.token_estimate >= threshold


async def compact_memory(
    memory: ShortTermMemory,
    transport: object,
    *,
    model: str,
    conv_store: ConversationStore | None = None,
) -> int:
    """Summarise *memory* in-place via a single LLM call.

    Replaces ``memory._messages`` with exactly two messages:
    - ``role:"user"`` — ``[COMPACT SUMMARY]\\n{summary}``
    - ``role:"assistant"`` — acknowledgement

    Sets ``conv_store.compaction_active`` to ``True`` before the LLM call and
    unconditionally restores it to ``False`` afterwards (even on error).

    Returns the new ``token_estimate`` after replacement.
    """
    from lauren_ai._transport import Message  # noqa: PLC0415

    if conv_store is not None:
        conv_store.compaction_active.set(True)
        conv_store.append_event("system", {"text": "⎋ Compacting conversation…"})

    try:
        import asyncio  # noqa: PLC0415
        # Yield to the event loop so the spinner repaint flushes to the terminal
        # before the LLM call begins.
        await asyncio.sleep(0)

        transcript = _format_transcript(memory._messages)

        completion = await transport.complete(
            [Message.user(transcript)],
            model=model,
            system=_COMPACT_SYSTEM,
            max_tokens=2048,
            temperature=0.0,
            stream=False,
        )
        summary: str = getattr(completion, "content", "") or ""

        memory._messages = [
            {"role": "user", "content": f"[COMPACT SUMMARY]\n{summary}"},
            {"role": "assistant", "content": _ACK},
        ]
        # PRD-129 Phase 2: compaction replaces _messages in place, bypassing the
        # JournaledShortTermMemory append/restore overrides — record the reset so
        # the durable journal stays in sync with the live buffer.
        _journal_reset = getattr(memory, "journal_reset", None)
        if callable(_journal_reset):
            _journal_reset()
        new_estimate = memory.token_estimate
        log.info("compactor: compacted to ~%d tokens", new_estimate)

        if conv_store is not None:
            conv_store.append_event(
                "system",
                {"text": f"⎋ Compacted → ~{new_estimate:,} tokens"},
            )
        return new_estimate

    except Exception as exc:  # noqa: BLE001
        log.warning("compactor: compaction failed: %s", exc)
        if conv_store is not None:
            conv_store.append_event(
                "system",
                {"text": f"⎋ Compaction failed: {type(exc).__name__}"},
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
