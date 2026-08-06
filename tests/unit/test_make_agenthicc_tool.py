"""Cache-contract coverage for the make_agenthicc_tool workflow."""

from __future__ import annotations

import pytest

from agenthicc.workflows.make_agenthicc_tool.runner import (
    CACHE_CONTRACT,
    MakeToolContext,
    MakeToolRunner,
    MakeToolState,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_every_make_tool_phase_uses_the_same_stable_cache_contract() -> None:
    """Retries and phase transitions must not rebuild the stable prefix."""

    runner = object.__new__(MakeToolRunner)
    calls: list[dict[str, object]] = []

    async def fake_run_phase(**kwargs: object) -> None:
        calls.append(kwargs)
        tools = kwargs.get("tools", [])
        assert isinstance(tools, list)
        tool_names = {getattr(tool, "__name__", "") for tool in tools}

        if "submit_tool_plan" in tool_names:
            tool = next(
                tool for tool in tools if getattr(tool, "__name__", "") == "submit_tool_plan"
            )
            await tool(tool_name="demo", description="A demo tool")
        elif "confirm_generation_complete" in tool_names:
            tool = next(
                tool
                for tool in tools
                if getattr(tool, "__name__", "") == "confirm_generation_complete"
            )
            await tool(file_path=".agenthicc/tools/demo.py", summary="generated")
        elif "approve_tool" in tool_names:
            tool = next(tool for tool in tools if getattr(tool, "__name__", "") == "approve_tool")
            await tool(summary="validated")

    runner.run_phase = fake_run_phase  # type: ignore[method-assign]
    context = MakeToolContext(intent="create a demo tool")
    memory = object()

    assert await runner._analyze(context, memory) is MakeToolState.GENERATE
    assert await runner._generate(context, memory) is MakeToolState.VALIDATE
    assert await runner._validate(context, memory) is MakeToolState.FINALIZE
    assert await runner._finalize(context, memory) is MakeToolState.COMPLETE

    assert len(calls) == 4
    assert all(call["stable_system_prompt"] == CACHE_CONTRACT for call in calls)
    assert "demo" not in CACHE_CONTRACT
    assert "validation report" in CACHE_CONTRACT
