"""Unit coverage for PRD-143 busy-command classification and metadata."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.commands import (
    BusyPolicy,
    Command,
    UnifiedCommandRegistry,
    build_builtin_registry,
    classify_busy_command,
)
from agenthicc.tui.trigger import TriggerContext
from agenthicc.tui.triggers.slash_command import SlashCommandTrigger

pytestmark = pytest.mark.unit


def test_builtins_have_explicit_immediate_read_only_inventory() -> None:
    registry = build_builtin_registry()

    immediate = {
        name
        for name in (
            "/clear",
            "/commands",
            "/expand",
            "/help",
            "/history",
            "/mcp",
            "/model",
            "/models",
            "/skills",
            "/status",
            "/usage",
        )
        if registry.get(name) is not None
    }
    assert immediate == {
        "/clear",
        "/commands",
        "/expand",
        "/help",
        "/history",
        "/mcp",
        "/model",
        "/models",
        "/skills",
        "/status",
        "/usage",
    }
    assert registry.get("/cancel").busy_policy is BusyPolicy.IMMEDIATE_CONTROL  # type: ignore[union-attr]
    assert registry.get("/interrupt") is registry.get("/cancel")
    assert registry.get("/workflow").busy_policy is BusyPolicy.QUEUE  # type: ignore[union-attr]
    assert registry.get("/compact").busy_policy is BusyPolicy.QUEUE  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("text", "policy"),
    [
        ("/usage", BusyPolicy.IMMEDIATE_READ_ONLY),
        ("/status", BusyPolicy.IMMEDIATE_READ_ONLY),
        ("/skills", BusyPolicy.IMMEDIATE_READ_ONLY),
        ("/skills reload", BusyPolicy.QUEUE),
        ("/commands reload", BusyPolicy.QUEUE),
        ("/mcp status", BusyPolicy.IMMEDIATE_READ_ONLY),
        ("/mcp connect https://example.test", BusyPolicy.QUEUE),
        ("/model", BusyPolicy.IMMEDIATE_READ_ONLY),
        ("/model openai gpt-5", BusyPolicy.QUEUE),
        ("/workflow demo", BusyPolicy.QUEUE),
        ("/compact", BusyPolicy.QUEUE),
        ("ordinary request", BusyPolicy.QUEUE),
    ],
)
def test_builtin_busy_decisions_are_subcommand_aware(text: str, policy: BusyPolicy) -> None:
    decision = classify_busy_command(text, build_builtin_registry())
    assert decision.policy is policy


def test_classification_is_pure_and_does_not_invoke_handler() -> None:
    calls: list[str] = []
    registry = UnifiedCommandRegistry()
    registry.register(
        Command(
            "/inspect",
            "Inspect",
            handler=lambda ctx: calls.append(ctx.text) or True,
            busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        )
    )

    decision = classify_busy_command("/inspect now", registry)

    assert decision.is_immediate
    assert decision.command_name == "/inspect"
    assert calls == []


def test_plugins_default_to_queue_and_cannot_claim_control_lane() -> None:
    registry = UnifiedCommandRegistry()
    registry.register(
        Command(
            "/plugin-control",
            "Plugin control",
            source_id="plugin:demo",
            busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
        )
    )

    decision = classify_busy_command("/plugin-control", registry)

    assert decision.policy is BusyPolicy.QUEUE
    assert "control lane" in decision.reason
    assert (
        Command("/new-plugin", "New plugin", source_id="plugin:new").busy_policy is BusyPolicy.QUEUE
    )


def test_broken_resolver_fails_closed_to_queue() -> None:
    def broken(_args: str) -> BusyPolicy:
        raise RuntimeError("resolver must not escape")

    registry = UnifiedCommandRegistry()
    registry.register(
        Command(
            "/broken",
            "Broken policy",
            busy_policy_resolver=broken,
            busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        )
    )

    decision = classify_busy_command("/broken", registry)

    assert decision.policy is BusyPolicy.QUEUE
    assert "failed safely" in decision.reason


def test_busy_picker_labels_immediate_and_queued_commands() -> None:
    trigger = SlashCommandTrigger(build_builtin_registry())
    ctx = TriggerContext(cwd=Path("."), busy=True)

    matches = {item.value: item for item in trigger.get_matches("", ctx)}

    assert "runs now" in matches["/usage"].hint
    assert "queues while busy" in matches["/workflow"].hint
    assert "[runs now]" in matches["/usage"].detail
