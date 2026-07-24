"""CLI commands for installing skills into project or user scope."""

from __future__ import annotations

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command, group


@group("skills", help="Install and manage skill definitions")
def _skills_group() -> None: ...


@command("skills", "add", help="Download and install a skill")
async def skills_add(
    ctx: CLIContext,
    source: str,
    global_: bool = False,
    project: bool = False,
    name: str = "",
) -> None:
    """Install SOURCE into project ``.agenthicc/skills`` or user-global skills."""
    from agenthicc.skills.installer import SkillInstallError, install_skill  # noqa: PLC0415

    try:
        result = await install_skill(
            source,
            global_scope=global_,
            project_scope=project,
            name=name,
        )
    except SkillInstallError as exc:
        print(f"error: {exc}")
        return
    print(f"Installed skill {result.slug} ({result.scope}) at {result.path}")
