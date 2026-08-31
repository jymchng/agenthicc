"""Shared transport-retry helper with scoped memory recovery (PRD-126/182).

``run_with_transport_retry`` handles atomic/stateless calls and remains
available to workflow phases, composite calls, direct TUI turns, and subagent
workers. It snapshots the conversation memory before each callable attempt and
restores it on a transient network error. Multi-step streaming callers can
provide a checkpoint for the latest committed provider step, allowing
agenthicc to retry with installed lauren-ai versions that have no internal
provider-step retry loop.

Bounds and safety:

- **max_retries** — hard attempt cap.
- **max_total_duration_s** — wall-clock ceiling across all attempts (0 = off).
- **deadline_monotonic** — absolute ``time.monotonic()`` deadline; when a turn
  timeout wraps the caller, this prevents scheduling a retry that cannot
  meaningfully run before the timeout fires.
- **jitter** — spreads simultaneous retries to avoid a thundering herd.
- **reset_fns** — side-effect rollback callbacks (e.g. approval-state reset)
  invoked after the memory restore, before the next attempt.
- **on_retry** — observability callback (sync or async) fired once per retry.

``CancelledError`` / ``KeyboardInterrupt`` are never retried.  Permanent
errors (anything not classified transient by ``_is_transient_network_error``)
propagate immediately.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agenthicc.runners.agent_turn import _is_transient_network_error

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory

__all__ = ["RetryConfig", "run_with_transport_retry"]

#: Minimum execution window (seconds) that must remain before a turn-timeout
#: deadline for a retry to be worth scheduling.
_MIN_EXEC_WINDOW_S: float = 2.0


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Return a finite, non-negative provider retry-after hint, if present."""
    for candidate in (exc, getattr(exc, "__cause__", None), getattr(exc, "__context__", None)):
        if candidate is None:
            continue
        value = getattr(candidate, "retry_after", None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            seconds = float(value)
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
    return None


@dataclass(frozen=True)
class RetryConfig:
    """Bounds for ``run_with_transport_retry``.

    :param max_retries: Maximum retry attempts after the first try.  ``0``
        disables retry (one attempt only).
    :param base_delay_s: First backoff delay; doubles each attempt.
    :param max_total_duration_s: Wall-clock ceiling across all attempts.
        ``0.0`` = no cap (bounded only by ``max_retries`` × per-call timeout).
    :param jitter: When ``True``, each delay is multiplied by a random factor
        in ``[0.75, 1.25]`` to avoid synchronised retries.
    """

    max_retries: int = 3
    base_delay_s: float = 1.0
    max_total_duration_s: float = 0.0
    jitter: bool = True


async def run_with_transport_retry(
    turn_fn: Callable[[], Awaitable[None]],
    *,
    config: RetryConfig,
    memory: ShortTermMemory | None = None,
    deadline_monotonic: float | None = None,
    on_retry: Callable[[int, int, float, BaseException], Awaitable[None] | None] | None = None,
    reset_fns: Sequence[Callable[[], None]] = (),
    checkpoint_provider: Callable[[], object | None] | None = None,
) -> None:
    """Run *turn_fn* with scoped retry on transient network errors.

    :param turn_fn: Zero-arg coroutine performing one full agent turn.  It is
        responsible for any memory mutation. For atomic callables, the
        snapshot/restore here guarantees each attempt sees a clean history.
        A multi-step caller must provide ``checkpoint_provider`` and advance it
        only after a committed step.
    :param config: Retry bounds.
    :param memory: ``ShortTermMemory`` to snapshot/restore.  When ``None`` (or
        lacking ``snapshot``/``restore``) no rollback is performed — retry
        still works for stateless callables.
    :param deadline_monotonic: Optional absolute ``time.monotonic()`` deadline;
        a retry is skipped (error re-raised) if it could not run before it.
    :param on_retry: Optional callback ``(attempt, max_retries, delay, exc)``
        fired before each backoff sleep.  May be sync or async.
    :param reset_fns: Side-effect rollback callbacks run after memory restore.
    :param checkpoint_provider: Optional callback returning the latest safe
        snapshot. When supplied, transient retry restores this snapshot rather
        than the snapshot taken before the whole callable. This is required
        when a callable contains multiple internally committed steps.
    :raises BaseException: The last error when retries are exhausted, or
        immediately for permanent / cancellation errors.
    """
    start = time.monotonic()
    can_snapshot = memory is not None and hasattr(memory, "snapshot") and hasattr(memory, "restore")

    for attempt in range(config.max_retries + 1):
        snapshot = memory.snapshot() if can_snapshot else None  # type: ignore[union-attr]

        try:
            await turn_fn()
            return

        except (asyncio.CancelledError, KeyboardInterrupt):
            raise

        except BaseException as exc:  # noqa: BLE001
            if not _is_transient_network_error(exc) or attempt >= config.max_retries:
                raise

            # 1. Roll back only the uncommitted portion. A step-aware caller
            # advances this checkpoint after each committed provider step;
            # falling back to the attempt snapshot preserves old atomic-call
            # behavior for stateless/one-shot users of this helper.
            recovery_snapshot = (
                checkpoint_provider() if checkpoint_provider is not None else snapshot
            )
            if recovery_snapshot is not None and memory is not None:
                memory.restore(recovery_snapshot)

            # 2. Roll back side effects (approval state, etc.).
            for fn in reset_fns:
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass

            # 3. Compute backoff with optional jitter.
            delay = config.base_delay_s * (2**attempt)
            retry_after = _retry_after_seconds(exc)
            if retry_after is not None:
                # Providers use this hint to protect rate limits. Never retry
                # before the requested window has elapsed; exponential
                # backoff remains the fallback for errors without a hint.
                delay = max(delay, retry_after)
            if config.jitter and delay > 0:
                jittered = delay * random.uniform(0.75, 1.25)  # noqa: S311 — not security-sensitive
                delay = max(retry_after or 0.0, jittered)

            now = time.monotonic()

            # 4. Total-duration ceiling.
            if (
                config.max_total_duration_s > 0
                and (now - start) + delay > config.max_total_duration_s
            ):
                raise

            # 5. Turn-timeout deadline awareness — don't schedule a retry that
            #    cannot meaningfully run before the deadline.
            if (
                deadline_monotonic is not None
                and (now + delay + _MIN_EXEC_WINDOW_S) >= deadline_monotonic
            ):
                raise

            # 6. Observability / user notification.
            if on_retry is not None:
                result = on_retry(attempt + 1, config.max_retries, delay, exc)
                if inspect.isawaitable(result):
                    await result

            await asyncio.sleep(delay)
