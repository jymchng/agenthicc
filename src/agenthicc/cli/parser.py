"""CLI argument parser for the ``agenthicc`` command."""
from __future__ import annotations

import argparse


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agenthicc",
        description="Agenthicc — state-driven agent OS for autonomous software engineering",
    )
    parser.add_argument("--headless", action="store_true",
                        help="Run without the TUI; emit JSON-lines to stdout.")
    parser.add_argument("--config", metavar="PATH", default=None,
                        help="Path to agenthicc.toml.")
    parser.add_argument("--version", action="version", version="agenthicc 0.1.0")
    parser.add_argument("--continue", dest="continue_session", action="store_true",
                        help="Continue the most recent session for this directory.")
    parser.add_argument("--resume", metavar="ID", default=None,
                        help="Resume the session with the given ID.")

    parser.add_argument(
        "--set",
        metavar="KEY=VALUE",
        action="append",
        default=[],
        dest="set_overrides",
        help="Override a config key (section.key=value). Can be repeated.",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("login",   help="Authenticate with agenthicc.ai")
    subparsers.add_parser("logout",  help="Log out and revoke tokens")
    subparsers.add_parser("whoami",  help="Show current authenticated user")
    subparsers.add_parser("sessions", help="List saved sessions")

    config_parser = subparsers.add_parser("config", help="Manage configuration")
    config_sub = config_parser.add_subparsers(dest="config_command")
    config_sub.add_parser("show", help="Print the merged effective configuration")
    config_init_p = config_sub.add_parser("init", help="Create a template agenthicc.toml")
    config_init_p.add_argument("--force", action="store_true", help="Overwrite existing config")

    return parser.parse_args()
