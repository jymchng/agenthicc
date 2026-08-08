"""Create the minimal project scaffold for a new Agenthicc project."""

from __future__ import annotations

from pathlib import Path

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command
from agenthicc.project_bootstrap import BootstrapError, initialize_project


@command("init", help="Create AGENTS.md and a commented .agenthicc configuration template")
def init_project(ctx: CLIContext, write: bool = False, force: bool = False) -> None:
    """Create the project scaffold; preserve existing files unless forced.

    ``--write`` remains accepted as a compatibility alias for callers of the
    former preview/write command.  Initialization now writes by default.
    """

    try:
        result = initialize_project(Path.cwd(), force=force)
    except BootstrapError as exc:
        print(f"error: {exc}")
        return

    print(f"Initialized {result.root}")
    for path in result.created:
        print(f"Created {path.relative_to(result.root)}")
    for path in result.overwritten:
        print(f"Overwrote {path.relative_to(result.root)}")
    for path in result.preserved:
        print(f"Preserved {path.relative_to(result.root)}")
    if write:
        print("Note: --write is retained for compatibility; init now writes by default.")
