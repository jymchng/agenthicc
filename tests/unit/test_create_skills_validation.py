"""Verify the shared skill loader and the stricter authoring contract."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from lauren_ai._memory import ShortTermMemory

from agenthicc.skills.loader import discover_skills_with_diagnostics, validate_skill_dir
from agenthicc.workflows.authoring.budget import RepairBudget
from agenthicc.workflows.create_skills import CreateSkills, CreateSkillsContext, CreateSkillsState
from agenthicc.workflows.create_skills.plan import SkillPlanError, decode_skill_plan, plan_skills
from agenthicc.workflows.create_skills.validation import validate_skill_set

pytestmark = pytest.mark.unit

_PLAN = {
    "slug": "summarize-release",
    "name": "Summarize Release",
    "description": "Summarize the release.",
    "when_to_use": ["release notes"],
    "tools": ["read_file"],
}
_BODY = """\
---
name: Summarize Release
description: Summarize the release.
suggestedTopics: [release notes]
tools: [read_file]
---
Read the release notes and summarize the changes.
"""


@pytest.mark.parametrize(
    ("delta", "code"),
    [
        ({"slug": "../bad"}, "invalid_slug"),
        ({"description": "x" * 301}, "invalid_description"),
        ({"when_to_use": ["one " * 13]}, "invalid_topics"),
        ({"aliases": ["summarize-release"]}, "invalid_alias"),
        ({"support_files": ["../guide.md"]}, "invalid_files"),
        ({"model": "other"}, "model_reason_required"),
        ({"tools": ["run_command"]}, "tool_not_available"),
        ({"disabled_tools": ["shell"]}, "tool_not_available"),
        ({"update": True}, "name_collision"),
    ],
)
def test_plan_refuses_invalid_or_widening_fields(delta: dict[str, object], code: str) -> None:
    with pytest.raises(SkillPlanError) as fault:
        plan_skills(
            [{**_PLAN, **delta}],
            reserved=frozenset(),
            owned=frozenset(),
            allowed_tools=frozenset({"read_file"}),
        )
    assert fault.value.code == code


@settings(max_examples=200)
@given(
    slug=st.from_regex(r"[a-z][a-z0-9]{0,9}(?:-[a-z]+)?", fullmatch=True),
    suffix=st.sampled_from(("", "missing-delimiter", "invalid-type", "invalid-key", "legacy")),
)
def test_validate_and_discover_agree_on_each_generated_skill(slug: str, suffix: str) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "project" / "skills" / slug
        path.mkdir(parents=True)
        source = {
            "": _BODY,
            "missing-delimiter": "---\nname: missing end",
            "invalid-type": _BODY.replace("tools: [read_file]", "tools: false"),
            "invalid-key": _BODY.replace("name:", "2:"),
            "legacy": "Legacy text-only skill.\n",
        }[suffix]
        (path / "SKILL.md").write_text(source, encoding="utf-8")
        checked = validate_skill_dir(path, source="project")
        found = discover_skills_with_diagnostics(
            project_dir=root / "project", user_dir=root / "user"
        )
        assert checked.ok == (
            slug in found.skills and all(item.severity != "error" for item in found.diagnostics)
        )
        assert checked.diagnostics == found.diagnostics


def test_report_records_loader_error_and_rejects_the_set(tmp_path: Path) -> None:
    candidate = tmp_path / "summarize-release"
    candidate.mkdir()
    (candidate / "SKILL.md").write_text("---\nname: Unterminated\n", encoding="utf-8")
    reports, candidates, inventories = validate_skill_set(tmp_path, (decode_skill_plan(_PLAN),))
    assert not candidates
    assert inventories["summarize-release"] == (".agenthicc/skills/summarize-release/SKILL.md",)
    (finding,) = reports["summarize-release"].findings
    assert finding.severity == "error"
    assert "has no closing delimiter" in finding.message


def test_checkpoint_restores_plan_and_inventory_without_serializing_memory(tmp_path: Path) -> None:
    plan = decode_skill_plan(_PLAN)
    candidate = tmp_path / plan.slug
    candidate.mkdir()
    (candidate / "SKILL.md").write_text(_BODY, encoding="utf-8")
    reports, candidates, inventories = validate_skill_set(tmp_path, (plan,))
    assert set(candidates) == {plan.slug}
    assert reports[plan.slug].errors == 0
    context = CreateSkillsContext(
        "Summarize the release.",
        "draft",
        RepairBudget(5, 3),
        state=CreateSkillsState.FINALIZE,
        phase_iteration=4,
        plans=(plan,),
        reports=reports,
        files=inventories,
    )
    memory = ShortTermMemory()
    data = json.loads(json.dumps(CreateSkills.checkpoint_context_to_payload(context)))
    restored = CreateSkills.checkpoint_context_from_payload(data, memory)
    assert restored.state is context.state
    assert restored.shared_memory is memory
    assert restored.repair == context.repair
    assert restored.plans == context.plans
    assert restored.files == context.files
    assert restored.reports == context.reports
