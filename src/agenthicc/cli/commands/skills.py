"""CLI commands for installing skills into project or user scope."""

from __future__ import annotations

from agenthicc.cli.context import CLIContext


async def skills_add(
    ctx: CLIContext,
    source: str,
    global_: bool = False,
    project: bool = False,
    name: str = "",
    skill: str = "",
    all_: bool = False,
) -> None:
    """Install SOURCE into project or user-global skills."""
    from agenthicc.skills.installer import SkillInstallError, install_skills

    try:
        requested_skills = (
            () if all_ else tuple(item.strip() for item in skill.split(",") if item.strip())
        )
        results = await install_skills(
            source,
            global_scope=global_,
            project_scope=project,
            name=name,
            skill_names=requested_skills,
        )
    except SkillInstallError as exc:
        print(f"error: {exc}")
        return
    for result in results:
        print(f"Installed skill {result.slug} ({result.scope}) at {result.path}")
