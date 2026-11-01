"""Record-only helpers for generated workflow phase transitions."""

from __future__ import annotations

import asyncio

MAX_TRANSITION_SUMMARY_CHARS: int = 4_000


def record_phase_transition(
    event: asyncio.Event,
    data: dict[str, object],
    *,
    outcome: str,
    summary: str,
) -> dict[str, object]:
    """Record a fixed outcome and bounded summary, then signal the phase runner.

    This helper deliberately has no workspace, artifact, service, or callback
    dependencies. A successful result records only that the agent submitted a
    transition; it does not verify the phase work.
    """
    if not isinstance(summary, str):
        return {"ok": False, "error": "The transition summary must be text."}
    normalized_summary = summary.strip()[:MAX_TRANSITION_SUMMARY_CHARS].rstrip()
    if not normalized_summary:
        return {"ok": False, "error": "The transition summary must not be empty."}
    if not isinstance(outcome, str) or not outcome.strip() or len(outcome) > 128:
        return {"ok": False, "error": "The transition outcome is invalid."}
    if event.is_set():
        return {"ok": False, "error": "A phase transition was already recorded."}

    data["outcome"] = outcome
    data["summary"] = normalized_summary
    event.set()
    return {"ok": True, "outcome": outcome, "summary_chars": len(normalized_summary)}
