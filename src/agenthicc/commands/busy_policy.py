"""Pure classification for commands submitted while a run is active."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .command import BusyPolicy

if TYPE_CHECKING:
    from .registry import UnifiedCommandRegistry

__all__ = ["BusyDecision", "classify_busy_command"]


@dataclass(frozen=True)
class BusyDecision:
    """The side-effect-free result of classifying one submitted input."""

    policy: BusyPolicy
    command_name: str = ""
    reason: str = ""

    @property
    def is_immediate(self) -> bool:
        return self.policy in (
            BusyPolicy.IMMEDIATE_READ_ONLY,
            BusyPolicy.IMMEDIATE_CONTROL,
        )


def classify_busy_command(text: str, registry: "UnifiedCommandRegistry") -> BusyDecision:
    """Classify *text* without invoking a handler or performing I/O.

    Unknown input remains queueable, preserving the existing FIFO behaviour.
    A resolver exception or invalid policy fails closed to the queue lane.
    """
    stripped = text.strip()
    parts = stripped.split(None, 1)
    token = parts[0] if parts else ""
    args = parts[1] if len(parts) > 1 else ""

    if not token.startswith(("/", "$")):
        return BusyDecision(
            BusyPolicy.QUEUE,
            reason="ordinary messages wait for the active run to finish",
        )

    command = registry.get(token)
    if command is None:
        return BusyDecision(
            BusyPolicy.QUEUE,
            command_name=token,
            reason="unknown commands retain the normal FIFO queue behaviour",
        )

    # Keep the source-aware skill namespace guard aligned with the dispatcher.
    # A stale slash-named skill record must not become an immediate command.
    if command.is_skill != token.startswith("$"):
        return BusyDecision(
            BusyPolicy.QUEUE,
            command_name=token,
            reason="namespace-mismatched commands remain deferred",
        )

    try:
        policy = command.policy_for_args(args)
        if not isinstance(policy, BusyPolicy):
            raise ValueError("busy policy resolver returned an invalid policy")
    except Exception as exc:  # noqa: BLE001
        return BusyDecision(
            BusyPolicy.QUEUE,
            command_name=token,
            reason=f"policy evaluation failed safely ({type(exc).__name__})",
        )

    # The control lane is reserved for the built-in control owner until a
    # separately reviewed plugin control contract exists.  A plugin that
    # accidentally labels a command as control therefore cannot interrupt or
    # detach the active session.
    if policy is BusyPolicy.IMMEDIATE_CONTROL and command.source_id != "builtin":
        return BusyDecision(
            BusyPolicy.QUEUE,
            command_name=token,
            reason="custom commands cannot claim the immediate control lane",
        )

    return BusyDecision(policy, command_name=token)
