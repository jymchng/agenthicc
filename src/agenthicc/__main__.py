"""Entry point for ``python -m agenthicc`` and the ``agenthicc`` command."""

from __future__ import annotations

import asyncio

from agenthicc.cli.parser import parse_cli
from agenthicc.cli.registry import _call
from agenthicc.runners.headless import _run_headless
from agenthicc.runners.tui_session import _run_tui


def main() -> None:
    ctx, ns = parse_cli()
    if entry := getattr(ns, "_entry", None):
        _call(entry, ctx, ns)
        return
    if ctx.headless:
        asyncio.run(_run_headless(ctx))
    else:
        _run_tui(ctx)


if __name__ == "__main__":
    main()
