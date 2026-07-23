"""Project guidance bootstrap command (PRD-139)."""

from __future__ import annotations

from pathlib import Path

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command
from agenthicc.project_bootstrap import (
    BootstrapError,
    BootstrapWriteError,
    build_bootstrap_plan,
    write_bootstrap_plan,
)


@command("init", help="Inspect the project and preview an AGENTS.md bootstrap")
def init_project(ctx: CLIContext, write: bool = False, force: bool = False) -> None:
    """Preview or explicitly write a managed project-guidance section."""

    try:
        plan = build_bootstrap_plan(Path.cwd())
    except BootstrapError as exc:
        print(f"error: {exc}")
        return

    preview = plan.preview()
    if preview:
        print(preview, end="" if preview.endswith("\n") else "\n")

    if not plan.changed:
        return
    if not write:
        print(
            "Preview only. Review the diff, then run `agenthicc init --write` to create AGENTS.md."
        )
        return
    if plan.exists and not force:
        print(
            "Refusing to overwrite existing AGENTS.md. Review the diff, then run "
            "`agenthicc init --write --force`."
        )
        return

    try:
        target = write_bootstrap_plan(plan, force=force)
    except BootstrapWriteError as exc:
        print(f"error: {exc}")
        return
    print(f"Updated {target}")
