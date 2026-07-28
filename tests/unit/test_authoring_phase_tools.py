"""Coverage for tool-gated authoring phases and installed API inspection."""

from __future__ import annotations

import asyncio

import pytest

from agenthicc.workflows.authoring.inspection_tools import make_authoring_inspection_tools
from agenthicc.workflows.authoring.phase_tools import (
    authoring_transition_tool_name,
    make_authoring_review_tools,
    make_authoring_transition_tools,
)
from agenthicc.workflows.authoring.state import AuthoringState, state_for_phase

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_design_transition_tool_only_signals_phase_completion() -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    (complete,) = make_authoring_transition_tools("design", event, data)

    assert (await complete("not yet"))["ok"] is True
    assert event.is_set()
    assert data["phase"] == "design"


@pytest.mark.asyncio
async def test_execute_transition_tool_captures_artifact_metadata() -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    (complete,) = make_authoring_transition_tools("execute", event, data)

    result = await complete("source is ready", "cloakbrowser_parse_fb", "Parse Facebook.")

    assert result["ok"] is True
    assert data["artifact_name"] == "cloakbrowser_parse_fb"
    assert data["artifact_description"] == "Parse Facebook."


@pytest.mark.asyncio
async def test_transition_tool_returns_actionable_validator_feedback() -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}

    def reject_transition(_data: dict[str, object]) -> tuple[str, str]:
        return "the staged artifact is missing", "stage the candidate before validating it"

    (complete,) = make_authoring_transition_tools("stage", event, data, validator=reject_transition)

    result = await complete("I checked the candidate")

    assert result["ok"] is False
    assert "staged artifact is missing" in str(result["error"])
    assert "stage the candidate" in str(result["error"])
    assert result["fix"] == "stage the candidate before validating it"
    assert result["retry"] is True
    assert not event.is_set()
    assert "last_error" in data


@pytest.mark.parametrize(
    ("phase_name", "expected_name"),
    [
        ("interpret", "complete_interpret_phase"),
        ("design", "complete_design_phase"),
        ("execute", "complete_execute_phase"),
        ("stage", "complete_stage_phase"),
        ("review", "request_publication_approval"),
        ("publish", "complete_publish_phase"),
        ("summarize", "complete_summarize_phase"),
    ],
)
def test_authoring_transition_tools_have_unique_phase_names(
    phase_name: str, expected_name: str
) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    tools = make_authoring_transition_tools(phase_name, event, data)
    metadata = getattr(tools[0], "__lauren_ai_tool__")

    assert authoring_transition_tool_name(phase_name) == expected_name
    assert metadata.name == expected_name


@pytest.mark.asyncio
async def test_review_tool_reports_denial_and_signals_runner() -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}

    async def deny() -> bool:
        return False

    (request_approval,) = make_authoring_review_tools(deny, event, data)

    assert getattr(request_approval, "__lauren_ai_tool__").name == ("request_publication_approval")
    result = await request_approval()

    assert result["ok"] is False
    assert result["rejected"] is True
    assert "Do not publish" in str(result["fix"])
    assert data == {"approval_decided": True, "approved": False}
    assert event.is_set()


def test_authoring_state_names_are_explicit() -> None:
    assert state_for_phase("interpret") is AuthoringState.INTERPRET
    assert state_for_phase("design") is AuthoringState.DESIGN
    assert AuthoringState.COMPLETE.is_terminal is True
    with pytest.raises(ValueError):
        state_for_phase("unknown")


@pytest.mark.asyncio
async def test_inspection_tools_read_docs_and_current_source() -> None:
    docs, source = make_authoring_inspection_tools()

    doc_result = await docs("guides/workflows.md", 5_000)
    assert doc_result["ok"] is True
    assert "create_workflow" in str(doc_result["content"])

    source_result = await source("agenthicc.workflows.plugin", "PhaseSpec", 2_000)
    assert source_result["ok"] is True
    assert "class PhaseSpec" in str(source_result["source"])
    assert "max_turns" in str(source_result["source"])

    private_result = await source("agenthicc.workflows.plugin", "_parse_output_schema", 2_000)
    assert private_result["ok"] is True
    assert "def _parse_output_schema" in str(private_result["source"])

    assert (await docs("../README.md"))["ok"] is False
    assert (await source("os", "path"))["ok"] is False
    assert (await source("agenthicc.workflows.plugin", "not-a-symbol"))["ok"] is False
