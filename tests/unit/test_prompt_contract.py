"""Unit tests for the cache-stable workflow prompt contract (PRD-163)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from lauren_ai._memory import ShortTermMemory
from lauren_ai._tools import tool

from agenthicc.runners.agent_turn import AgentTurnRunner
from agenthicc.runners.agent_turn_context import AgentTurnContext
from agenthicc.runners.prompt_contract import (
    CACHE_CONTRACT_VERSION,
    DEFAULT_WORKFLOW_CACHE_POLICY,
    PromptBlock,
    build_prompt_contract,
    build_workflow_prompt_contract,
)

pytestmark = pytest.mark.unit


@tool()
async def stable_read(path: str) -> dict[str, object]:
    """Read a stable path."""

    return {"path": path}


@tool()
async def phase_transition(summary: str) -> dict[str, object]:
    """Complete the current phase."""

    return {"summary": summary}


def test_dynamic_phase_changes_do_not_change_stable_fingerprint() -> None:
    first = build_workflow_prompt_contract(
        workflow_name="demo",
        phase_prompt="Plan the work.",
        stable_tools=[stable_read],
        phase_tools=[phase_transition],
        execution=SimpleNamespace(
            provider="anthropic",
            effective_model=lambda: "model-a",
            profile="",
            base_url="",
        ),
    )
    second = build_workflow_prompt_contract(
        workflow_name="demo",
        phase_prompt="Validate a completely different artifact.",
        stable_tools=[stable_read],
        phase_tools=[phase_transition],
        execution=SimpleNamespace(
            provider="anthropic",
            effective_model=lambda: "model-a",
            profile="",
            base_url="",
        ),
    )

    assert first.stable_fingerprint == second.stable_fingerprint
    assert first.cache_epoch.value == second.cache_epoch.value
    assert first.dynamic_fingerprint != second.dynamic_fingerprint
    assert "Validate a completely different artifact." in second.render_dynamic_message("run")


def test_connection_identity_changes_epoch_but_not_stable_prompt_fingerprint() -> None:
    first = build_prompt_contract(
        stable_system_prefix="policy",
        provider="openai",
        model="model-a",
        connection_identity={"base_url_digest": "one"},
    )
    second = build_prompt_contract(
        stable_system_prefix="policy",
        provider="openai",
        model="model-a",
        connection_identity={"base_url_digest": "two"},
    )

    assert first.stable_fingerprint == second.stable_fingerprint
    assert first.cache_epoch.value != second.cache_epoch.value
    assert first.connection_fingerprint != second.connection_fingerprint


def test_stable_policy_contains_question_contract_and_is_immutable() -> None:
    contract = build_workflow_prompt_contract(
        workflow_name="questions_demo",
        phase_prompt="Current phase.",
        stable_tools=[stable_read],
    )

    assert CACHE_CONTRACT_VERSION in contract.contract_version
    assert "ask_user" in contract.stable_system_prefix
    assert "clarifying" in contract.stable_system_prefix
    assert "do not guess" in contract.stable_system_prefix.lower()
    assert DEFAULT_WORKFLOW_CACHE_POLICY in contract.stable_system_prefix
    assert "a real answer" not in contract.stable_system_prefix


def test_tool_order_is_stable_first_then_phase_local_and_deterministic() -> None:
    contract = build_prompt_contract(
        stable_system_prefix="policy",
        dynamic_system_context=[PromptBlock("PHASE", "dynamic")],
        stable_tools=[stable_read],
        phase_tools=[phase_transition],
    )

    ordered = contract.ordered_tools([phase_transition, stable_read])
    assert [getattr(tool, "__name__", "") for tool in ordered] == [
        "stable_read",
        "phase_transition",
    ]
    assert contract.diagnostics()["stable_tool_names"] == ["stable_read"]


def test_overlapping_or_conflicting_tool_schemas_fail_closed() -> None:
    with pytest.raises(ValueError, match="both stable and phase-local"):
        build_prompt_contract(stable_tools=[stable_read], phase_tools=[stable_read])

    async def other(path: str, extra: str) -> dict[str, object]:
        return {"path": path, "extra": extra}

    other.__name__ = "stable_read"
    with pytest.raises(ValueError, match="conflicting schemas"):
        build_prompt_contract(stable_tools=[stable_read, other])


def test_diagnostics_are_redacted_and_do_not_include_dynamic_prompt_content() -> None:
    contract = build_prompt_contract(
        stable_system_prefix="stable policy",
        dynamic_system_context=[
            PromptBlock("PHASE", "private artifact content that must not be logged")
        ],
        stable_tools=[stable_read],
        connection_identity={"base_url": "https://private.example"},
    )

    diagnostics = contract.diagnostics()
    rendered = repr(diagnostics)
    assert "private artifact content" not in rendered
    assert "private.example" not in rendered
    assert all(isinstance(value, (str, int, list)) for value in diagnostics.values())


def test_summary_is_rendered_as_dynamic_context() -> None:
    contract = build_workflow_prompt_contract(
        workflow_name="summary_demo",
        phase_prompt="Continue.",
        stable_tools=[stable_read],
    )

    rendered = contract.render_dynamic_message(
        "continue the task",
        extra_blocks=[PromptBlock("EARLIER CONVERSATION SUMMARY", "old answer")],
    )
    assert "old answer" in rendered
    assert "old answer" not in contract.stable_system_prefix


@pytest.mark.parametrize(
    ("provider", "status"),
    [
        ("anthropic", "eligible"),
        ("modal", "eligible"),
        ("ollama", "unsupported"),
        ("future", "unknown"),
    ],
)
def test_provider_capability_never_claims_unknown_or_unsupported_cache_hits(
    provider: str, status: str
) -> None:
    contract = build_prompt_contract(provider=provider, stable_system_prefix="policy")
    assert contract.cache_status == status


def test_agent_turn_hides_summary_from_system_prompt_and_restores_memory() -> None:
    memory = ShortTermMemory(max_tokens=1_000)
    memory.set_summary("summary that must stay dynamic")
    contract = build_workflow_prompt_contract(
        workflow_name="summary_demo",
        phase_prompt="phase instructions",
        stable_tools=[stable_read],
    )
    context = AgentTurnContext(
        text="continue",
        runner=MagicMock(),
        processor=MagicMock(),
        session_memory=memory,
        prompt_contract=contract,
    )
    runner = AgentTurnRunner(context)
    runner._runtime_dynamic_blocks = [PromptBlock("TOOLS", "stable tools")]

    rendered = runner._prepare_cache_stable_message("continue")

    assert memory.summary is None
    assert "summary that must stay dynamic" in rendered
    assert "summary that must stay dynamic" not in contract.stable_system_prefix
    runner._restore_cache_summary()
    assert memory.summary == "summary that must stay dynamic"
