"""CLI argument parser — builds argparse from the command index (PRD-79, AGX-01)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agenthicc.cli.context import CLIContext, CLIFlags
from agenthicc.cli.version import package_version

if TYPE_CHECKING:
    from agenthicc.config import AgenthiccConfig


class _VersionAction(argparse.Action):
    """``--version`` that reads package metadata only when it is used.

    argparse's built-in ``version`` action needs the string when the parser is
    built, which would import ``importlib.metadata`` for every command.
    """

    def __init__(
        self,
        option_strings: list[str],
        dest: str = argparse.SUPPRESS,
        default: str = argparse.SUPPRESS,
        help: str = "show program's version number and exit",
    ) -> None:
        super().__init__(
            option_strings=option_strings, dest=dest, default=default, nargs=0, help=help
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[object] | None,
        option_string: str | None = None,
    ) -> None:
        sys.stdout.write(f"agenthicc {package_version()}\n")
        parser.exit()


def _add_global_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--goal",
        metavar="GOAL",
        default=None,
        help=(
            "Select goal_flow and submit GOAL as its initial user intent; "
            "cannot be combined with --workflow."
        ),
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="Return after a --goal run is durably accepted by the background supervisor.",
    )
    parser.add_argument(
        "--priority",
        choices=("urgent", "normal", "low"),
        default="normal",
        dest="goal_priority",
        help="Queue priority for a detached --goal run (default: normal).",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit machine-readable output for a goal start or control operation.",
    )
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
            "Start the TUI with NAME selected, or run NAME for each stdin line in headless "
            "mode; unavailable with --goal."
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
    parser.add_argument("--version", action=_VersionAction)
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
        print(f"agenthicc {package_version()}")
        raise SystemExit(0)
    if argv in (["--help"], ["-h"]):
        _fast_help_parser().parse_args(argv)
        raise AssertionError("argparse help should terminate")

    from agenthicc.cli import registry

    # Parse only global options first.  This gives trusted project-command
    # discovery the same configuration snapshot later consumed by the runner,
    # and leaves the command path at the front of the remaining arguments.
    bootstrap_parser = argparse.ArgumentParser(add_help=False)
    _add_global_flags(bootstrap_parser)
    bootstrap_ns, remaining = bootstrap_parser.parse_known_args(argv)
    config = None
    try:
        from agenthicc.config import load_config

        config = load_config(
            cli_overrides=list(getattr(bootstrap_ns, "set_overrides", [])),
            cli_secret_overrides=list(getattr(bootstrap_ns, "set_secret_overrides", [])),
            config_path=getattr(bootstrap_ns, "config", None),
        )
    except Exception:  # noqa: BLE001
        pass

    registry.discover_plugins(
        strict_cli_shadow=bool(
            getattr(getattr(config, "plugins", None), "strict_cli_shadow", False)
        ),
    )
    index = registry.command_index()

    parser = argparse.ArgumentParser(
        prog="agenthicc",
        description="Agenthicc — state-driven agent OS for autonomous software engineering",
    )
    _add_global_flags(parser)
    # Only the selected command's module is imported; every other node is a
    # help-only stub (AGX-01).
    registry.wire(parser, index, index.resolve(registry.leading_tokens(remaining)))

    ns = parser.parse_args()
    ns_values = vars(ns)
    if ns_values.get("detach", False) and not ns_values.get("goal", None):
        parser.error("--detach requires --goal GOAL")
    if ns_values.get("goal", None) and ns_values.get("_entry", None) is not None:
        parser.error("--goal cannot be combined with a subcommand")
    if ns_values.get("goal", None) and ns_values.get("workflow_name", None):
        parser.error("--goal always runs goal_flow and cannot be combined with --workflow")
    ctx = _build_ctx(ns, config=config)
    return ctx, ns


def _fast_help_parser() -> argparse.ArgumentParser:
    """Build help from the built-in manifest without importing any command."""
    from agenthicc.cli import registry

    parser = argparse.ArgumentParser(
        prog="agenthicc",
        description="Agenthicc — state-driven agent OS for autonomous software engineering",
        epilog="Project and user command plugins are discovered when a command runs.",
    )
    _add_global_flags(parser)
    registry.wire(parser, registry.command_index(), None)
    return parser


def _build_ctx(
    ns: argparse.Namespace,
    *,
    config: object | None = None,
) -> CLIContext:
    typed_config = cast("AgenthiccConfig | None", config)
    goal_priority = vars(ns).get("goal_priority", "normal")
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
        goal=vars(ns).get("goal"),
        goal_priority=goal_priority if isinstance(goal_priority, str) else "normal",
        detach=bool(vars(ns).get("detach", False)),
        json_output=bool(vars(ns).get("json_output", False)),
        config=typed_config,
    )
