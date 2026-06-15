"""Entry point for ``python -m agenthicc`` and the ``agenthicc`` command."""
from __future__ import annotations

import asyncio

from agenthicc.cli.parser import _parse_args
from agenthicc.cli.config import _do_config_show, _do_config_init
from agenthicc.cli.auth import _do_login, _do_logout, _do_whoami
from agenthicc.sessions import _do_sessions
from agenthicc.runners.headless import _run_headless
from agenthicc.runners.tui_session import _run_tui


def main() -> None:
    args = _parse_args()

    if args.command == "config":
        if getattr(args, "config_command", None) == "show":
            _do_config_show(args)
        elif getattr(args, "config_command", None) == "init":
            _do_config_init(args)
        else:
            print("Usage: agenthicc config [show|init]")
        return
    elif args.command == "login":
        asyncio.run(_do_login())
    elif args.command == "logout":
        asyncio.run(_do_logout())
    elif args.command == "whoami":
        _do_whoami()
    elif args.command == "sessions":
        _do_sessions()
    elif args.headless:
        asyncio.run(_run_headless())
    else:
        _run_tui(args)


if __name__ == "__main__":
    main()
