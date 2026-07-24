"""Unit coverage for the ``agenthicc skills add`` installer."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import pytest

from agenthicc.cli.context import CLIContext
from agenthicc.skills import installer
from agenthicc.skills.installer import SkillInstallError, install_skill
from agenthicc.skills.loader import discover_skills

pytestmark = pytest.mark.unit


def _source_skill(root: Path, name: str = "demo") -> Path:
    source = root / name
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: Demo\ndescription: A downloaded demo skill\n---\n\nDo demo work.\n",
        encoding="utf-8",
    )
    return source


def test_install_local_skill_to_project_scope_and_discover_it(tmp_path: Path) -> None:
    source = _source_skill(tmp_path / "source")
    project = tmp_path / "project"

    result = asyncio.run(install_skill(str(source), project_dir=project))

    assert result.slug == "demo"
    assert result.scope == "project"
    assert result.path == project / "skills" / "demo"
    discovered = discover_skills(project_dir=project, user_dir=tmp_path / "user")
    assert discovered["demo"].description == "A downloaded demo skill"


def test_install_local_skill_to_global_scope(tmp_path: Path) -> None:
    source = _source_skill(tmp_path / "source", "source-name")
    user = tmp_path / "user"

    result = asyncio.run(install_skill(str(source), global_scope=True, user_dir=user))

    assert result.scope == "global"
    assert result.path == user / "skills" / "source-name"
    assert result.path.joinpath("SKILL.md").is_file()


def test_install_remote_source_uses_downloaded_content_and_explicit_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_remote(source: str) -> tuple[str, str]:
        assert source == "https://example.test/skill/SKILL.md"
        return "---\nname: Remote\n---\n\nRemote instructions.\n", "skill"

    monkeypatch.setattr(installer, "_remote_source", fake_remote)
    result = asyncio.run(
        install_skill(
            "https://example.test/skill/SKILL.md",
            name="remote-skill",
            project_dir=tmp_path / "project",
        )
    )

    assert result.path.name == "remote-skill"
    assert "Remote instructions." in result.path.joinpath("SKILL.md").read_text()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "https://github.com/acme/skills/tree/main/review-code",
            "https://raw.githubusercontent.com/acme/skills/main/review-code/SKILL.md",
        ),
        (
            "https://github.com/acme/skills/blob/main/review-code/SKILL.md",
            "https://raw.githubusercontent.com/acme/skills/main/review-code/SKILL.md",
        ),
    ],
)
def test_github_skill_url_normalization(source: str, expected: str) -> None:
    assert installer._github_raw_url(source) == expected


def test_invalid_source_and_scope_are_rejected_without_writing(tmp_path: Path) -> None:
    with pytest.raises(SkillInstallError, match="only one target"):
        asyncio.run(
            install_skill(
                "missing",
                global_scope=True,
                project_scope=True,
                project_dir=tmp_path / "project",
            )
        )

    with pytest.raises(SkillInstallError, match="HTTPS"):
        asyncio.run(install_skill("http://example.test/SKILL.md", project_dir=tmp_path))

    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "SKILL.md").write_text("---\nname: [bad]\n---\n", encoding="utf-8")
    with pytest.raises(SkillInstallError, match="metadata validation"):
        asyncio.run(install_skill(str(invalid), project_dir=tmp_path / "project"))
    assert not (tmp_path / "project" / "skills" / "invalid").exists()


def test_existing_skill_is_never_overwritten(tmp_path: Path) -> None:
    source = _source_skill(tmp_path / "source")
    project = tmp_path / "project"
    target = project / "skills" / "demo"
    target.mkdir(parents=True)
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(SkillInstallError, match="already exists"):
        asyncio.run(install_skill(str(source), project_dir=project))
    assert marker.read_text(encoding="utf-8") == "keep"


def test_cli_parser_supports_reserved_global_flag_name() -> None:
    from agenthicc.cli import registry

    old_registry = registry._REGISTRY.copy()
    old_groups = registry._GROUPS.copy()
    registry._REGISTRY.clear()
    registry._GROUPS.clear()
    try:

        @registry.command("skills", "add", help="add a skill")
        def add(ctx: CLIContext, global_: bool = False) -> None:
            return None

        parser = argparse.ArgumentParser()
        registry._wire(parser, registry._as_tree())
        namespace = parser.parse_args(["skills", "add", "--global"])
        assert namespace.global_ is True
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(old_registry)
        registry._GROUPS.clear()
        registry._GROUPS.update(old_groups)


def test_cli_handler_reports_install_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from agenthicc.cli.commands import skills

    async def fail(*args: object, **kwargs: object) -> object:
        raise SkillInstallError("bad skill source")

    monkeypatch.setattr(installer, "install_skill", fail)
    asyncio.run(skills.skills_add(CLIContext(), "source"))
    assert "error: bad skill source" in capsys.readouterr().out
