"""Entry point for ``python -m agenthicc`` and the ``agenthicc`` command."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, cast

from agenthicc.cli.parser import parse_cli

if TYPE_CHECKING:
    from agenthicc.cli.context import CLIContext


def _call(*args: object, **kwargs: object) -> None:
    """Lazy compatibility wrapper for CLI command dispatch."""
    from agenthicc.cli.registry import _call as dispatch

    typed_dispatch = cast("Callable[..., object]", dispatch)
    typed_dispatch(*args, **kwargs)


def _run_headless(ctx: object) -> Coroutine[object, object, None]:
    """Lazy compatibility wrapper for the headless runner."""
    from agenthicc.runners.headless import _run_headless as run_headless

    return run_headless(cast("CLIContext", ctx))


def _run_tui(ctx: object) -> None:
    """Lazy compatibility wrapper for the TUI runner."""
    from agenthicc.runners.tui_session import _run_tui as run_tui

    run_tui(cast("CLIContext", ctx))


def main() -> None:
    ctx, ns = parse_cli()
    if entry := getattr(ns, "_entry", None):
        dispatch = cast("Callable[..., None]", _call)
        dispatch(entry, ctx, ns)
        return
    if ctx.headless:
        asyncio.run(_run_headless(ctx))
    else:
        _run_tui(ctx)


if __name__ == "__main__":
    main()
