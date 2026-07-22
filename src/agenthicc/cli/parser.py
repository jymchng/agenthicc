"""CLI argument parser — builds argparse from the decorator registry (PRD-79)."""

from __future__ import annotations

import argparse
from pathlib import Path

from agenthicc.cli.context import CLIContext, CLIFlags


def _add_global_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without the TUI; emit JSON-lines to stdout.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Path to agenthicc.toml.",
    )
    parser.add_argument("--version", action="version", version="agenthicc 0.1.0")
    parser.add_argument(
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Continue the most recent session for this directory.",
    )
    parser.add_argument(
        "--resume",
        metavar="ID",
        default=None,
        help="Resume the session with the given ID.",
    )
    parser.add_argument(
        "--record-cassette",
        metavar="DIR",
        nargs="?",
        const=str(Path.home() / ".agenthicc" / "cassettes"),
        default=None,
        dest="record_cassette",
        help=(
            "Record LLM calls and approvals to DIR/<session-id>/. "
            "Omit DIR to use ~/.agenthicc/cassettes."
        ),
    )
    parser.add_argument(
        "--set",
        metavar="KEY=VALUE",
        action="append",
        default=[],
        dest="set_overrides",
        help="Override a config key (section.key=value). Can be repeated.",
    )
    parser.add_argument(
        "--dangerously-skip-permissions",
        dest="dangerously_skip_permissions",
        action="store_true",
        default=False,
        help=(
            "Disable ALL tool approval prompts for this session. "
            "Overrides Guard mode and all per-mode approval requirements. "
            "Intentionally not settable in agenthicc.toml."
        ),
    )


def parse_cli() -> tuple[CLIContext, argparse.Namespace]:
    """Discover commands, build argparse, and return (CLIContext, Namespace)."""
    from agenthicc.cli.registry import _discover, _as_tree, _wire  # noqa: PLC0415

    # Read strict_cli_shadow before discovery so conflicts are handled correctly.
    strict = False
    try:
        from agenthicc.config import load_config  # noqa: PLC0415

        strict = load_config().plugins.strict_cli_shadow
    except Exception:  # noqa: BLE001
        pass

    _discover(strict_cli_shadow=strict)

    parser = argparse.ArgumentParser(
        prog="agenthicc",
        description="Agenthicc — state-driven agent OS for autonomous software engineering",
    )
    _add_global_flags(parser)
    _wire(parser, _as_tree())

    ns = parser.parse_args()
    ctx = _build_ctx(ns)
    return ctx, ns


def _build_ctx(ns: argparse.Namespace) -> CLIContext:
    flags = CLIFlags(
        dangerously_skip_permissions=getattr(ns, "dangerously_skip_permissions", False),
    )
    return CLIContext(
        resume_id=getattr(ns, "resume", None),
        headless=getattr(ns, "headless", False),
        config_path=getattr(ns, "config", None),
        set_overrides=tuple(getattr(ns, "set_overrides", [])),
        flags=flags,
        record_cassette=getattr(ns, "record_cassette", None),
        continue_session=getattr(ns, "continue_session", False),
    )


# ── backward-compat shim ──────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    """Legacy shim — returns just the Namespace for callers that haven't migrated."""
    _, ns = parse_cli()
    return ns
