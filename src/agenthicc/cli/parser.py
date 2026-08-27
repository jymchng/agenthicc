"""CLI argument parser — builds argparse from the decorator registry (PRD-79)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agenthicc.cli.context import CLIContext, CLIFlags

if TYPE_CHECKING:
    from agenthicc.config import AgenthiccConfig


def _add_global_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without the TUI; emit JSON-lines to stdout.",
    )
    parser.add_argument(
        "--workflow",
        metavar="NAME",
        default=None,
        dest="workflow_name",
        help=(
            "Start the TUI with NAME selected, or run NAME for each stdin line in headless mode."
        ),
    )
    parser.add_argument(
        "--mode",
        metavar="MODE",
        default=None,
        dest="mode_name",
        help="Start with MODE selected (for example: Safe, Plan, or Yolo).",
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
        "--set-secret",
        metavar="KEY=ENV_VAR",
        action="append",
        default=[],
        dest="set_secret_overrides",
        help=(
            "Set a secret config key from an environment variable "
            "(section.key=ENV_VAR). Can be repeated."
        ),
    )
    parser.add_argument(
        "--dangerously-skip-permissions",
        dest="dangerously_skip_permissions",
        action="store_true",
        default=False,
        help=(
            "Disable ALL tool approval prompts for this session. "
            "Overrides Safe mode approval requirements. Plan mode hard blocks "
            "side effects even with this flag. "
            "Intentionally not settable in agenthicc.toml."
        ),
    )


def parse_cli() -> tuple[CLIContext, argparse.Namespace]:
    """Discover commands, build argparse, and return (CLIContext, Namespace)."""
    argv = sys.argv[1:]
    # Help/version are intentionally handled before registry/config discovery.
    # This keeps documentation queries deterministic and prevents project
    # extension code, optional integrations, and durable stores from loading
    # just to answer a process-local question.
    if argv == ["--version"]:
        print("agenthicc 0.1.0")
        raise SystemExit(0)
    if argv in (["--help"], ["-h"]):
        _fast_help_parser().parse_args(argv)
        raise AssertionError("argparse help should terminate")

    from agenthicc.cli.registry import _discover, _as_tree, _wire  # noqa: PLC0415

    # Parse only global options first.  This gives trusted project-command
    # discovery the same configuration snapshot later consumed by the runner,
    # without executing dynamic command modules to learn those options.
    bootstrap_parser = argparse.ArgumentParser(add_help=False)
    _add_global_flags(bootstrap_parser)
    bootstrap_ns, _ = bootstrap_parser.parse_known_args(argv)
    config = None
    try:
        from agenthicc.config import load_config  # noqa: PLC0415

        config = load_config(
            cli_overrides=list(getattr(bootstrap_ns, "set_overrides", [])),
            cli_secret_overrides=list(getattr(bootstrap_ns, "set_secret_overrides", [])),
            config_path=getattr(bootstrap_ns, "config", None),
        )
    except Exception:  # noqa: BLE001
        pass

    _discover(
        strict_cli_shadow=bool(
            getattr(getattr(config, "plugins", None), "strict_cli_shadow", False)
        ),
        config=config,
    )

    parser = argparse.ArgumentParser(
        prog="agenthicc",
        description="Agenthicc — state-driven agent OS for autonomous software engineering",
    )
    _add_global_flags(parser)
    _wire(parser, _as_tree())

    ns = parser.parse_args()
    ctx = _build_ctx(ns, config=config)
    return ctx, ns


def _fast_help_parser() -> argparse.ArgumentParser:
    """Build help without importing dynamic command implementations."""
    parser = argparse.ArgumentParser(
        prog="agenthicc",
        description="Agenthicc — state-driven agent OS for autonomous software engineering",
        epilog=(
            "Built-in command groups: agents, auth, config, jobs, mcp, sessions, "
            "skills, tools, trust, and workflows. Project commands are discovered "
            "after startup."
        ),
    )
    _add_global_flags(parser)
    return parser


def _build_ctx(
    ns: argparse.Namespace,
    *,
    config: object | None = None,
) -> CLIContext:
    typed_config = cast("AgenthiccConfig | None", config)
    flags = CLIFlags(
        dangerously_skip_permissions=getattr(ns, "dangerously_skip_permissions", False),
    )
    return CLIContext(
        resume_id=getattr(ns, "resume", None),
        headless=getattr(ns, "headless", False),
        config_path=getattr(ns, "config", None),
        set_overrides=tuple(getattr(ns, "set_overrides", [])),
        set_secret_overrides=tuple(getattr(ns, "set_secret_overrides", [])),
        flags=flags,
        record_cassette=getattr(ns, "record_cassette", None),
        continue_session=getattr(ns, "continue_session", False),
        workflow_name=getattr(ns, "workflow_name", None),
        mode_name=getattr(ns, "mode_name", None),
        config=typed_config,
    )


# ── backward-compat shim ──────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    """Legacy shim — returns just the Namespace for callers that haven't migrated."""
    _, ns = parse_cli()
    return ns
