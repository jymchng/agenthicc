"""Contract tests for reconstruct_site's Markdown research handoffs."""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path

import pytest
from lauren_ai._tools._schema import generate_tool_schema

from agenthicc.workflows.reconstruct_site.phase_impl import (
    ReconstructContext,
    ReconstructState,
    ReconstructSiteWorkflow as PhaseWorkflow,
    _make_architecture_tools,
    _make_content_assets_tools,
    _make_design_system_tools,
    _make_interaction_analysis_tools,
    _make_recon_tools,
    _make_research_gate_tools,
    _make_responsive_research_tools,
    _make_visual_research_tools,
    _research_notes_path,
    _research_notes_prompt,
)


@pytest.mark.parametrize(
    ("factory", "tool_name"),
    (
        (_make_recon_tools, "submit_route_inventory"),
        (_make_visual_research_tools, "submit_visual_spec"),
        (_make_interaction_analysis_tools, "submit_interaction_inventory"),
        (_make_content_assets_tools, "submit_asset_inventory"),
        (_make_responsive_research_tools, "submit_responsive_research"),
        (_make_architecture_tools, "submit_architecture"),
        (_make_design_system_tools, "submit_design_system"),
    ),
)
def test_research_submission_tools_expose_only_required_summary(
    factory: object, tool_name: str
) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    tool = next(
        item
        for item in factory(event, data)  # type: ignore[operator]
        if getattr(item, "__name__", "") == tool_name
    )

    signature = inspect.signature(tool)

    assert tuple(signature.parameters) == ("summary",)
    assert signature.parameters["summary"].default is inspect.Parameter.empty


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "factory",
    (
        _make_recon_tools,
        _make_visual_research_tools,
        _make_interaction_analysis_tools,
        _make_content_assets_tools,
        _make_responsive_research_tools,
        _make_architecture_tools,
        _make_design_system_tools,
    ),
)
async def test_summary_handoff_is_the_only_transition_side_effect(
    factory: object, tmp_path: Path
) -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    tool = factory(event, data)[0]  # type: ignore[operator]

    assert (await tool(summary="  findings are in Markdown  "))["ok"] is True
    assert data == {"summary": "findings are in Markdown"}
    assert event.is_set()
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.asyncio
async def test_empty_summary_does_not_transition() -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    tool = _make_recon_tools(event, data)[0]

    result = await tool(summary="   ")

    assert result["ok"] is False
    assert not event.is_set()
    assert data == {}


@pytest.mark.asyncio
async def test_gate_validator_rejects_pending_approval_without_signalling_transition() -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    tools = _make_research_gate_tools(
        event,
        data,
        validator=lambda _action, _payload: "coverage is still pending",
    )

    result = await tools[0](summary="Approve this baseline", baseline_artifact_id="baseline")

    assert result["ok"] is False
    assert result["error"] == "coverage is still pending"
    assert not event.is_set()
    assert data == {}


@pytest.mark.asyncio
async def test_summary_only_gate_can_accept_empty_degraded_exception_set() -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    tools = _make_research_gate_tools(
        event,
        data,
        validator=lambda _action, _payload: None,
    )

    result = await tools[1](
        exception_ids=[],
        rationale="No browser cells are being asserted in summary-only mode.",
        baseline_artifact_id="baseline",
    )

    assert result["ok"] is True
    assert event.is_set()
    assert data["action"] == "approve_degraded"


def test_reconstruct_research_tools_have_warning_free_input_schemas(
    caplog: pytest.LogCaptureFixture,
) -> None:
    factories = (
        _make_recon_tools,
        _make_visual_research_tools,
        _make_interaction_analysis_tools,
        _make_content_assets_tools,
        _make_responsive_research_tools,
        _make_research_gate_tools,
        _make_architecture_tools,
        _make_design_system_tools,
    )

    with caplog.at_level(logging.WARNING, logger="lauren_ai._tools._schema"):
        for factory in factories:
            event = asyncio.Event()
            for tool in factory(event, {}):  # type: ignore[arg-type]
                generate_tool_schema(tool)

    assert not [
        record for record in caplog.records if "unrecognised type annotation" in record.message
    ]


def test_research_prompt_names_exact_agent_owned_markdown_path() -> None:
    prompt = _research_notes_prompt("/tmp/site", "visual", "visual-observations.md")

    assert "/tmp/site/research/visual/" in prompt
    assert "/tmp/site/research/visual/visual-observations.md" in prompt
    assert "write_file" in prompt
    assert "does not create or validate" in prompt
    assert _research_notes_path("/tmp/site", "visual", "visual-observations.md") == (
        "/tmp/site/research/visual/visual-observations.md"
    )


def test_research_phase_metadata_uses_summary_only_tools_and_file_handoffs() -> None:
    expected = {
        "recon": ("submit_route_inventory(summary)", "reconnaissance/route-inventory.md"),
        "visual_research": (
            "submit_visual_spec(summary)",
            "visual/visual-observations.md",
        ),
        "interaction_analysis": (
            "submit_interaction_inventory(summary)",
            "interaction/interaction-analysis.md",
        ),
        "content_assets": (
            "submit_asset_inventory(summary)",
            "content_assets/asset-inventory.md",
        ),
        "responsive_research": (
            "submit_responsive_research(summary)",
            "responsive/responsive-observations.md",
        ),
        "architecture": ("submit_architecture(summary)", "architecture/architecture.md"),
        "design_system": (
            "submit_design_system(summary)",
            "design_system/design-system.md",
        ),
    }

    for phase in PhaseWorkflow.phases:
        if phase.name not in expected:
            continue
        tool_call, path = expected[phase.name]
        assert tool_call in phase.system_prompt_override
        assert path in phase.system_prompt_override
        assert "write_file" in phase.system_prompt_override


def test_submission_phase_metadata_requires_written_handoff_before_tool_call() -> None:
    for phase in PhaseWorkflow.phases:
        prompt = phase.system_prompt_override
        if "submit_" in prompt:
            assert "before calling" in prompt.lower()
            assert "write" in prompt.lower()


def test_phase_summaries_survive_checkpoint_round_trip() -> None:
    context = ReconstructContext(
        intent="reconstruct a site",
        run_id="handoff-run",
        state=ReconstructState.RESEARCH_GATE,
        target_directory="/tmp/site",
        phase_summaries={
            "recon": "Routes are recorded in Markdown.",
            "visual_research": "Measured visual findings are recorded in Markdown.",
        },
    )

    payload = PhaseWorkflow.checkpoint_context_to_payload(context)
    restored = PhaseWorkflow.checkpoint_context_from_payload(payload)

    assert restored.phase_summaries == context.phase_summaries
