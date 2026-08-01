"""Regression coverage for site_imitate phase handoff instructions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from agenthicc.workflows.site_imitate import (
    SiteImitateContext,
    SiteImitateRunner,
    SiteImitateState,
    SiteImitateWorkflow,
)
from agenthicc.workflows.site_imitate.runner import (
    MOBILE_RESPONSIVE_CONTRACT,
    _make_final_verify_tools,
    _make_verify_tools,
)

pytestmark = pytest.mark.unit


_EXPECTED_TOOLS: dict[str, tuple[str, ...]] = {
    "analyze": ("submit_analysis",),
    "plan": ("submit_plan", "request_reanalysis"),
    "scaffold": ("scaffold_complete",),
    "build": ("component_built",),
    "verify": ("component_verified", "component_verification_failed"),
    "final_verify": ("final_verify_passed", "final_verify_failed"),
}


def test_static_phase_prompts_name_their_transition_tools() -> None:
    prompts = {phase.name: phase.system_prompt_override for phase in SiteImitateWorkflow.phases}

    for phase_name, tool_names in _EXPECTED_TOOLS.items():
        prompt = prompts[phase_name]
        for tool_name in tool_names:
            assert tool_name in prompt, (phase_name, tool_name)
        assert "successful transition-tool call" in prompt
        assert "never advances" in prompt
        assert "mobile" in prompt.lower()


@pytest.mark.asyncio
async def test_runtime_phase_prompts_name_the_tools_the_runner_expects() -> None:
    runner = object.__new__(SiteImitateRunner)
    runner.total_phases = 4
    context = SiteImitateContext(
        intent="https://example.test | a dashboard",
        target_url="https://example.test",
        new_purpose="a dashboard",
        run_id="run",
        state=SiteImitateState.ANALYZE,
        shared_memory=object(),
        component_plan=[{"name": "Header", "build": "header", "verify": "renders"}],
    )
    prompts: dict[str, str] = {}
    stable_prompts: dict[str, str] = {}
    transition_args: dict[str, tuple[object, ...]] = {
        "submit_analysis": ("analysis", "inventory"),
        "submit_plan": ("plan", ["Header | header | renders"]),
        "scaffold_complete": ("/tmp/site",),
        "component_built": (),
        "component_verified": ("verified at mobile, tablet, and desktop viewports",),
        "final_verify_passed": ("built and responsive at mobile, tablet, and desktop viewports",),
    }

    async def fake_run_phase(
        *,
        system_prompt: str,
        tools: list[Callable[..., object]],
        **_kwargs: object,
    ) -> None:
        phase_name = next(
            name
            for name in sorted(_EXPECTED_TOOLS, key=len, reverse=True)
            if f"{name.replace('_', ' ').upper()} phase" in system_prompt
        )
        prompts[phase_name] = system_prompt
        stable_prompt = _kwargs.get("stable_system_prompt")
        assert stable_prompt == MOBILE_RESPONSIVE_CONTRACT
        stable_prompts[phase_name] = str(stable_prompt)
        for tool in tools:
            tool_name = getattr(tool, "__name__", "")
            if tool_name in transition_args:
                await tool(*transition_args[tool_name])  # type: ignore[operator]
                return
        raise AssertionError(f"no transition tool found for {phase_name}")

    runner.run_phase = fake_run_phase  # type: ignore[method-assign]

    assert await runner._analyze(context, object()) is SiteImitateState.PLAN
    assert await runner._plan(context, object()) is SiteImitateState.SCAFFOLD
    assert await runner._scaffold(context, object()) is SiteImitateState.BUILD
    assert await runner._build_component(context, object()) is SiteImitateState.VERIFY
    assert await runner._verify_component(context, object()) is SiteImitateState.FINAL_VERIFY
    assert await runner._final_verify(context, object()) is SiteImitateState.COMPLETE

    assert set(prompts) == set(_EXPECTED_TOOLS)
    for phase_name, tool_names in _EXPECTED_TOOLS.items():
        prompt = prompts[phase_name]
        assert all(tool_name in prompt for tool_name in tool_names), phase_name
        assert "successful" in prompt
        assert "never advances" in prompt
        assert "mobile" in prompt.lower()

    assert set(stable_prompts) == set(_EXPECTED_TOOLS)
    assert all("320px" in prompt and "horizontal" in prompt for prompt in stable_prompts.values())


@pytest.mark.asyncio
async def test_verification_tools_require_mobile_evidence() -> None:
    component_event = asyncio.Event()
    component_data: dict[str, str] = {}
    component_tools = _make_verify_tools(component_event, component_data)
    rejected = await component_tools[0]("TypeScript passes")
    assert rejected["ok"] is False  # type: ignore[index]
    assert not component_event.is_set()

    final_event = asyncio.Event()
    final_data: dict[str, str] = {}
    final_tools = _make_final_verify_tools(final_event, final_data)
    accepted = await final_tools[0]("Responsive mobile, tablet, and desktop viewports pass")
    assert accepted["ok"] is True  # type: ignore[index]
    assert final_event.is_set()
