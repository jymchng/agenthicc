"""Unit coverage for workflow phase-handoff prompt construction."""

from __future__ import annotations

import pytest

from agenthicc.tools.capabilities import tool_control, tool_read
from agenthicc.workflows.plugin import phase_transition_instruction

pytestmark = pytest.mark.unit


def _control_tool(name: str):
    """Build a control tool with a deliberately workflow-specific name."""

    async def transition() -> dict[str, bool]:
        return {"ok": True}

    transition.__name__ = name
    return tool_control(transition)


def _read_tool(name: str):
    """Build an ordinary tool that must not be presented as a handoff."""

    async def inspect() -> dict[str, bool]:
        return {"ok": True}

    inspect.__name__ = name
    return tool_read(inspect)


def test_control_metadata_advertises_custom_transition_tools() -> None:
    prompt = phase_transition_instruction(
        [_read_tool("read_repository"), _control_tool("publish_design")],
        phase_name="design",
    )

    assert "`publish_design`" in prompt
    assert "read_repository" not in prompt
    assert "only after a transition tool call succeeds" in prompt
    assert "prose" in prompt


def test_known_transition_name_remains_compatible_without_metadata() -> None:
    async def finalize_plan() -> None:
        return None

    prompt = phase_transition_instruction([finalize_plan], phase_name="plan")

    assert "`finalize_plan`" in prompt


def test_expected_names_narrow_shared_declarative_tool_groups() -> None:
    prompt = phase_transition_instruction(
        [
            _control_tool("request_plan_approval"),
            _control_tool("finalize_plan"),
            _control_tool("mark_execute_complete"),
        ],
        phase_name="plan",
        expected_tool_names=("request_plan_approval", "finalize_plan"),
    )

    assert "`request_plan_approval`" in prompt
    assert "`finalize_plan`" in prompt
    assert "mark_execute_complete" not in prompt


def test_missing_transition_tools_are_explicitly_described() -> None:
    prompt = phase_transition_instruction(
        [_read_tool("read_repository")],
        phase_name="summary",
    )

    assert "No phase-transition tool is available" in prompt
    assert "enclosing runner" in prompt


def test_question_tool_is_not_mislabelled_as_a_phase_transition() -> None:
    async def ask_user(questions: list[dict[str, object]]) -> dict[str, object]:
        return {"questions": questions}

    ask_user = tool_control(ask_user)
    prompt = phase_transition_instruction([ask_user], phase_name="design")

    assert "No phase-transition tool is available" in prompt
    assert "ask_user" not in prompt
