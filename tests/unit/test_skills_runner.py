"""Unit tests for skills runtime: context commands, arg substitution, auto-triggering (PRD-23)."""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from agenthicc.skills.runner import (
    render_context_commands,
    substitute_args,
    find_matching_skills,
    load_template,
    maybe_load_reference,
    process_skill_body,
)
from agenthicc.skills.loader import SkillDef, SkillPermissionSet

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill(slug: str, topics: list[str], disallow: bool = False) -> SkillDef:
    return SkillDef(
        name=slug,
        slug=slug,
        path=Path("."),
        suggested_topics=topics,
        disallow_auto_triggering=disallow,
    )


# ---------------------------------------------------------------------------
# PRD-specified tests (section 7)
# ---------------------------------------------------------------------------


def test_context_command_becomes_an_instruction_to_run_it():
    body = "Current dir:\n!`echo hello`"
    result = render_context_commands(body)
    assert result == (
        "Current dir:\n[run `echo hello` with your command tool and use its output here]"
    )


def test_a_skill_context_command_never_starts_a_process(tmp_path):
    """PRD-250 S-01: a repository skill cannot run a command by being processed."""
    marker = tmp_path / "pwned"
    skill = SkillDef(
        name="innocent",
        slug="innocent",
        path=tmp_path,
        source="project",
        suggested_topics=["status"],
        _body=f"Check the tree first: !`touch {marker}` then report status.",
    )

    body = process_skill_body(skill, args=[])

    assert not marker.exists()
    assert f"[run `touch {marker}` with your command tool" in body
    assert "!`" not in body


def test_the_skill_runner_cannot_spawn_processes():
    """The runtime has no process-spawning path left to reach."""
    import agenthicc.skills.runner as runner

    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "os.system" not in source


def test_substitute_positional_args():
    body = "Analyze {0} against {1}."
    result = substitute_args(body, ["src/", "main"])
    assert result == "Analyze src/ against main."


def test_substitute_missing_arg_gives_empty():
    body = "File: {0}, Branch: {1}"
    result = substitute_args(body, ["only_one"])
    assert result == "File: only_one, Branch: "


def test_substitute_session_and_effort():
    body = "session={session} effort={effort}"
    result = substitute_args(body, [], session_id="abc123", effort="high")
    assert result == "session=abc123 effort=high"


def test_substitute_full_args_preserves_user_instruction():
    body = "Create the requested feature: {args}"
    result = substitute_args(body, ["a", "tool", "for", "weather"])
    assert result == "Create the requested feature: a tool for weather"


def test_find_matching_skills_by_topic():
    skills = {
        "deploy": _make_skill("deploy", ["deploy", "release"]),
        "debug": _make_skill("debug", ["debug", "fix"]),
    }
    matched = find_matching_skills("please deploy to production", skills)
    assert any(s.slug == "deploy" for s in matched)
    assert all(s.slug != "debug" for s in matched)


def test_disallow_auto_triggering_prevents_match():
    skills = {
        "secret": _make_skill("secret", ["deploy"], disallow=True),
    }
    matched = find_matching_skills("deploy now", skills)
    assert matched == []


def test_agent_turn_injects_only_skills_allowed_for_active_agent(tmp_path):
    from agenthicc.runners.agent_turn import AgentTurnRunner
    from agenthicc.runners.agent_turn_context import AgentTurnContext

    review_dir = tmp_path / "review"
    deploy_dir = tmp_path / "deploy"
    review_dir.mkdir()
    deploy_dir.mkdir()
    (review_dir / "SKILL.md").write_text("---\nname: Review\n---\nReview instructions\n")
    (deploy_dir / "SKILL.md").write_text("---\nname: Deploy\n---\nDeploy instructions\n")
    skills = {
        "review": SkillDef("Review", "review", review_dir, suggested_topics=["review"]),
        "deploy": SkillDef("Deploy", "deploy", deploy_dir, suggested_topics=["deploy"]),
    }
    context = AgentTurnContext(
        text="review deploy",
        runner=None,
        processor=MagicMock(),
        skills=skills,
        active_agent="planner",
        skill_permissions=SkillPermissionSet(allowed_skills=frozenset({"review"})),
    )

    turn = AgentTurnRunner(context)
    turn._inject_skills()

    assert "Review instructions" in turn._skill_suffix
    assert "Deploy instructions" not in turn._skill_suffix


# ---------------------------------------------------------------------------
# Additional tests (beyond the PRD spec)
# ---------------------------------------------------------------------------


def test_every_context_command_placeholder_is_rendered():
    """Two !`cmd` expressions in the same body are both replaced."""
    body = "A: !`echo first` | B: !`echo second`"
    result = render_context_commands(body)
    assert "[run `echo first`" in result
    assert "[run `echo second`" in result
    assert "!`" not in result


def test_load_template_missing(tmp_path):
    """load_template returns empty string when template.md is absent."""
    result = load_template(tmp_path)
    assert result == ""


def test_load_template_exists(tmp_path):
    """load_template returns separator + content when template.md is present."""
    template_file = tmp_path / "template.md"
    template_file.write_text("## Output format\n- bullet list", encoding="utf-8")
    result = load_template(tmp_path)
    assert result.startswith("\n\n---\n\n")
    assert "Output format" in result
    assert "bullet list" in result


def test_reference_not_in_body(tmp_path):
    """maybe_load_reference leaves the body unchanged when {reference} is absent."""
    body = "No placeholder here."
    result = maybe_load_reference(body, tmp_path)
    assert result == body


def test_process_full_pipeline(tmp_path):
    """Create a tmp skill dir with SKILL.md and template.md; verify {0} is substituted
    and template is appended when process_skill_body is called."""
    # Set up skill directory with SKILL.md (no front matter, just body)
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("Analyze {0} carefully.", encoding="utf-8")

    template_md = tmp_path / "template.md"
    template_md.write_text("## Expected output", encoding="utf-8")

    # Build a mock skill whose body is the raw SKILL.md text and whose
    # path (used inside process_skill_body as the skill directory) points at tmp_path.
    mock_skill = MagicMock(spec=SkillDef)
    mock_skill.body = "Analyze {0} carefully."
    mock_skill.path = tmp_path  # runner.py uses skill.path as the skill directory

    result = process_skill_body(mock_skill, args=["src/"])

    assert "Analyze src/ carefully." in result
    assert "## Expected output" in result
