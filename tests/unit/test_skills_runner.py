"""Unit tests for skills runtime: context injection, arg substitution, auto-triggering (PRD-23)."""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from agenthicc.skills.runner import (
    inject_context,
    substitute_args,
    find_matching_skills,
    load_template,
    maybe_load_reference,
    process_skill_body,
)
from agenthicc.skills.loader import SkillDef

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


def test_inject_context_replaces_cmd(tmp_path):
    body = "Current dir:\n!`echo hello`"
    result = inject_context(body, cwd=tmp_path)
    assert "hello" in result
    assert "!`" not in result


def test_inject_context_failed_cmd(tmp_path):
    body = "!`nonexistent_command_xyz_abc`"
    result = inject_context(body, cwd=tmp_path)
    assert "context injection failed" in result or result == ""


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


# ---------------------------------------------------------------------------
# Additional tests (beyond the PRD spec)
# ---------------------------------------------------------------------------


def test_inject_context_multiple_placeholders(tmp_path):
    """Two !`cmd` expressions in the same body are both replaced."""
    body = "A: !`echo first` | B: !`echo second`"
    result = inject_context(body, cwd=tmp_path)
    assert "first" in result
    assert "second" in result
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

    result = process_skill_body(mock_skill, args=["src/"], cwd=tmp_path)

    assert "Analyze src/ carefully." in result
    assert "## Expected output" in result
