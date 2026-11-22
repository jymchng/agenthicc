"""Model skill permissions, rendering, and remaining-turn restrictions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType

import pytest

from agenthicc.runners.reload import SurfaceHolder
from agenthicc.skills.loader import SkillDef, SkillPermissionSet, discover_skills_with_diagnostics
from agenthicc.skills.runner import find_matching_skills
from agenthicc.skills.tools import (
    MAX_BODY_BYTES,
    SkillUseState,
    bind_skill_use,
    list_skills,
    use_skill,
)

pytestmark = pytest.mark.unit


def _write_skill(project: Path, slug: str, *, fields: str = "", body: str = "Guidance") -> Path:
    directory = project / "skills" / slug
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {slug}\n{fields}---\n{body}\n", encoding="utf-8"
    )
    return directory


async def test_model_skill_permissions_and_frontmatter(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_skill(project, "opted-out", fields="disable_model_invocation: true\n")
    _write_skill(project, "invalid", fields='disable_model_invocation: "false"\n')
    _write_skill(project, "other-model", fields="model: other-model\n")
    _write_skill(project, "denied", fields="denied_agents: [planner]\n")
    _write_skill(project, "reviewer-only", fields="allowed_agents: [reviewer]\n")
    _write_skill(
        project,
        "manual-trigger",
        fields=(
            "disable_model_invocation: false\ndisallow_auto_triggering: true\n"
            "suggested_topics: [manual]\nmax_turn_depth: 1\naliases: [choose]\n"
        ),
    )
    result = discover_skills_with_diagnostics(project_dir=project, user_dir=tmp_path / "user")
    assert any(item.code == "invalid-boolean" for item in result.diagnostics)
    assert not find_matching_skills("manual", result.skills)
    assert result.skills["manual-trigger"].max_turn_depth == 1
    surface = SurfaceHolder(MappingProxyType(result.skills))
    permissions = SkillPermissionSet(denied_skills=frozenset({"reviewer-only"}))
    state = SkillUseState(
        surface,
        agent="planner",
        permission_lookup=lambda agent: permissions if agent == "reviewer" else None,
    )

    with bind_skill_use(state):
        listing = await list_skills()
        assert {item["slug"]: item["model_invocable"] for item in listing} == {
            "manual-trigger": True,
            "other-model": False,
        }
        for slug, expected in {
            "opted-out": "not_model_invocable",
            "other-model": "not_model_invocable",
            "denied": "not_available_to_agent",
            "reviewer-only": "not_available_to_agent",
            "unknown": "unknown_skill",
        }.items():
            assert (await use_skill(slug))["code"] == expected
        assert (await use_skill("$choose"))["slug"] == "manual-trigger"
        with bind_skill_use(state.for_agent("reviewer")):
            assert (await use_skill("reviewer-only"))["code"] == "not_available_to_agent"
            assert (await use_skill("manual-trigger"))["ok"] is True
        assert "already_loaded" in await use_skill("manual-trigger")
    assert await list_skills() == []
    assert (await use_skill("manual-trigger"))["code"] == "not_available_to_agent"


async def test_bounded_body_digest_and_cumulative_narrowing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    body = "</skill>" + ("é" * 20_000)
    _write_skill(project, "large", body=body, fields="tools: [read, use_skill, not-granted]\n")
    _write_skill(project, "further", fields="disabled_tools: [read]\n")
    _write_skill(project, "large-inventory", fields="description: inventory\n")
    skills = discover_skills_with_diagnostics(
        project_dir=project, user_dir=tmp_path / "user"
    ).skills
    events: list[dict[str, object]] = []
    state = SkillUseState(
        SurfaceHolder(MappingProxyType(skills)), agent="auto", on_use=events.append
    )
    state.configure_tools(["read", "use_skill", "shell"])
    result = await state.use("large", [])
    expected_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert result["digest"] == expected_hash
    assert result["truncated"] is True
    assert result["narrowed_tools"] == ["read", "use_skill"]
    wrapped = result["body"]
    assert isinstance(wrapped, str)
    assert wrapped.startswith(
        f'<skill slug="large" source="project" digest="{expected_hash[:16]}">\n'
    )
    assert wrapped.count("</skill>") == 1
    assert len(wrapped.split("\n", 2)[1].encode()) <= MAX_BODY_BYTES

    assert await state.use("large", ["different"]) == {
        "ok": True,
        "already_loaded": {"slug": "large", "digest": expected_hash},
    }
    assert (await state.use("further", []))["narrowed_tools"] == ["use_skill"]
    assert [item["slug"] for item in events] == ["large", "further"]
    assert all("body" not in item for item in events)


def test_inventory_ranks_and_bounds_rendered_size(tmp_path: Path) -> None:
    skills = {
        f"ordinary-{i:02}": SkillDef("Ordinary", f"ordinary-{i:02}", tmp_path) for i in range(50)
    }
    skills["release"] = SkillDef("Release", "release", tmp_path, suggested_topics=["version"])
    surface = SurfaceHolder(MappingProxyType(skills))
    state = SkillUseState(surface, agent="auto")
    ranked = state.inventory("version")
    assert ranked[0]["slug"] == "release"
    assert len(ranked) == 40

    verbose = {slug: SkillDef("Verbose", slug, tmp_path, description="🌱" * 400) for slug in skills}
    rendered = SkillUseState(SurfaceHolder(MappingProxyType(verbose)), agent="auto").inventory(None)
    assert 0 < len(rendered) < 40
    assert len(json.dumps(rendered)) <= 6_000


@pytest.mark.skipif(os.name == "nt", reason="Creating a symlink on Windows requires privileges")
async def test_model_skill_cannot_read_a_symlinked_companion(tmp_path: Path) -> None:
    project = tmp_path / "project"
    directory = _write_skill(project, "linked", body="Read {reference}.")
    outside = tmp_path / "external.md"
    outside.write_text("private", encoding="utf-8")
    (directory / "reference.md").symlink_to(outside)
    skills = discover_skills_with_diagnostics(
        project_dir=project, user_dir=tmp_path / "user"
    ).skills
    state = SkillUseState(SurfaceHolder(MappingProxyType(skills)), agent="auto")
    assert not state.inventory(None)[0]["model_invocable"]
    result = await state.use("linked", [])
    assert result["code"] == "unknown_skill"
    assert "private" not in str(result)
