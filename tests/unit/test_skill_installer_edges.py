"""Boundary coverage for skill source parsing, cloning, and staging."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.skills import installer
from agenthicc.skills.installer import SkillInstallError

pytestmark = pytest.mark.unit


def test_skill_source_helpers_reject_bad_content_and_normalize_names(tmp_path: Path) -> None:
    with pytest.raises(SkillInstallError, match="too large"):
        installer._decode_skill(b"x" * (installer._MAX_SKILL_BYTES + 1), "source")
    with pytest.raises(SkillInstallError, match="UTF-8"):
        installer._decode_skill(b"\xff", "source")
    assert installer._decode_skill(b"valid", "source") == "valid"
    assert (
        installer._skill_root(global_scope=True, project_dir=tmp_path, user_dir=tmp_path / "user")
        == tmp_path / "user" / "skills"
    )
    assert (
        installer._skill_root(global_scope=False, project_dir=tmp_path, user_dir=None)
        == tmp_path / "skills"
    )
    assert installer._normalise_install_name("  My Skill  ", "fallback") == "my-skill"
    assert installer._normalise_install_name("", "Fallback") == "fallback"
    with pytest.raises(SkillInstallError, match="determine"):
        installer._normalise_install_name("", "")
    with pytest.raises(SkillInstallError, match="too long"):
        installer._normalise_install_name("x" * 1000, "fallback")


def test_local_source_and_github_parsers_cover_invalid_forms(tmp_path: Path) -> None:
    with pytest.raises(SkillInstallError, match="does not exist"):
        installer._local_source(str(tmp_path / "missing"))
    directory = tmp_path / "empty"
    directory.mkdir()
    with pytest.raises(SkillInstallError, match="SKILL.md"):
        installer._local_source(str(directory))
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("content", encoding="utf-8")
    assert installer._local_source(str(skill_file)) == ("content", tmp_path.name)

    assert installer._github_repository_source("http://github.com/a/b") is None
    assert installer._github_repository_source("https://example.com/a/b") is None
    assert installer._github_repository_source("https://github.com/a") is None
    assert installer._github_repository_source("./owner/repo") is None
    assert installer._repository_source(str(directory)).url == str(directory.resolve())
    assert installer._repository_source(str(skill_file)) is None
    with pytest.raises(SkillInstallError, match="SKILL.md"):
        installer._github_raw_url("http://example.com/not-skill")
    with pytest.raises(SkillInstallError, match="SKILL.md"):
        installer._github_raw_url("https://github.com/a/b/blob/main/skill")
    with pytest.raises(SkillInstallError, match="must use"):
        installer._github_raw_url("https://github.com/a/b/tree/main")
    assert installer._remote_name("https://example.com/a/SKILL.md") == "a"
    assert installer._remote_name("https://example.com/a") == "a"


def test_repository_discovery_selection_and_tree_safety(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    (repository / "skills" / "one").mkdir(parents=True)
    (repository / "skills" / "one" / "SKILL.md").write_text(
        "---\nname: One\ndescription: one\n---\nbody\n", encoding="utf-8"
    )
    (repository / ".git" / "ignored").mkdir(parents=True)
    (repository / ".git" / "ignored" / "SKILL.md").write_text("bad", encoding="utf-8")
    found = installer._discover_repository_skills(repository)
    assert [item.slug for item in found] == ["one"]
    assert installer._select_repository_skills(found, ()) == found
    assert installer._select_repository_skills(found, ("One",))[0].slug == "one"
    with pytest.raises(SkillInstallError, match="no matching"):
        installer._select_repository_skills(found, ("missing",))
    with pytest.raises(SkillInstallError, match="inside"):
        installer._discover_repository_skills(repository, "../outside")
    with pytest.raises(SkillInstallError, match="does not exist"):
        installer._discover_repository_skills(repository, "missing")


def test_local_repository_clone_copies_and_rejects_remote_protocols(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("body", encoding="utf-8")

    async def check() -> None:
        temporary, repository = await installer._clone_repository(
            installer._GitRepositorySource(str(source))
        )
        assert (repository / "file.txt").read_text() == "body"
        import shutil

        shutil.rmtree(temporary)
        with pytest.raises(SkillInstallError, match="HTTPS"):
            await installer._clone_repository(installer._GitRepositorySource("git://example/repo"))

    asyncio.run(check())


def test_clone_reports_git_failures_without_leaking_temp_directories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    monkeypatch.setattr(installer.tempfile, "mkdtemp", lambda **_kwargs: "/tmp/fake-skill-root")
    monkeypatch.setattr(
        installer.asyncio,
        "to_thread",
        lambda *_args, **_kwargs: _failed_process(),
    )

    async def check() -> None:
        with pytest.raises(SkillInstallError, match="clone failed"):
            await installer._clone_repository(
                installer._GitRepositorySource("https://github.com/a/b.git")
            )

    async def _failed_process() -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stderr="fatal: repository unavailable")

    asyncio.run(check())
    assert subprocess is not None


def test_install_directory_rejects_symlinks_and_invalid_metadata(tmp_path: Path) -> None:
    source_path = tmp_path / "source"
    source_path.mkdir()
    (source_path / "SKILL.md").write_text("---\nname: Source\n---\nbody\n", encoding="utf-8")
    discovered = installer._DiscoveredSkill(source_path, "source", "Source")
    root = tmp_path / "target"
    result = installer._install_directory(discovered, name="", root=root, global_scope=False)
    assert result.path.joinpath("SKILL.md").is_file()
    with pytest.raises(SkillInstallError, match="already exists"):
        installer._install_directory(discovered, name="", root=root, global_scope=False)

    symlink_source = tmp_path / "symlink"
    symlink_source.mkdir()
    (symlink_source / "SKILL.md").write_text("---\nname: Link\n---\n", encoding="utf-8")
    (symlink_source / "external.txt").symlink_to(source_path / "SKILL.md")
    with pytest.raises(SkillInstallError, match="symlink"):
        installer._install_directory(
            installer._DiscoveredSkill(symlink_source, "link", "Link"),
            name="link",
            root=tmp_path / "other",
            global_scope=False,
        )


@pytest.mark.asyncio
async def test_install_skills_rejects_empty_and_name_mismatch_sources(tmp_path: Path) -> None:
    with pytest.raises(SkillInstallError, match="required"):
        await installer.install_skill("  ", project_dir=tmp_path)
    with pytest.raises(SkillInstallError, match="only one target"):
        await installer.install_skills(
            str(tmp_path), global_scope=True, project_scope=True, project_dir=tmp_path
        )

    repository = tmp_path / "repository"
    (repository / "skills" / "one").mkdir(parents=True)
    (repository / "skills" / "two").mkdir()
    for name in ("one", "two"):
        (repository / "skills" / name / "SKILL.md").write_text(
            f"---\nname: {name}\n---\nbody\n", encoding="utf-8"
        )
    with pytest.raises(SkillInstallError, match="one repository"):
        await installer.install_skills(
            str(repository), name="named", project_dir=tmp_path / "project"
        )
