"""Phase tools are filtered by the phase's own mode, and replays control the catalog.

A workflow phase that declares a mode override compiles its tool list before
its turn switches modes. These tests pin the two seams that keep that safe:
``phase_mode`` resolves the override without activating it, and
``override_builtin_tools`` lets a cassette replay replace the built-in catalog
inside the registry, so no filter can expose a real tool behind a stub.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from lauren_ai._tools import tool

from agenthicc.config import AgenthiccConfig
from agenthicc.plugins.registry import ToolGroup, build_registry, override_builtin_tools
from agenthicc.tools.base import tool_name
from agenthicc.tools.capabilities import tool_read, tool_write
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import ModeManager, UnknownModeError, phase_mode
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.default.runner import WorkflowRunner
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

pytestmark = pytest.mark.unit


@tool_write
@tool()
async def stage_change(path: str) -> dict[str, str]:
    """A write-capable project tool."""
    return {"path": path}


@tool_read
@tool()
async def inspect_change(path: str) -> dict[str, str]:
    """A read-only project tool."""
    return {"path": path}


def _plan_manager() -> tuple[AppState, ModeManager, list[str]]:
    app = AppState.create()
    manager = ModeManager(app_state=app, default_name="Plan")
    published: list[str] = []
    manager.set_change_callback(lambda mode: published.append(mode.name))
    return app, manager, published


# ── phase_mode ────────────────────────────────────────────────────────────────


def test_phase_mode_resolves_an_override_without_activating_it() -> None:
    app, manager, published = _plan_manager()
    active = app.active_mode()

    resolved = phase_mode(active, manager, "Yolo")

    assert resolved.name == "Yolo"
    assert not resolved.blocked_capabilities
    assert active.blocked_capabilities
    assert app.active_mode().name == "Plan"
    assert manager.active_name == "Plan"
    assert published == []


def test_phase_mode_without_an_override_or_manager_is_the_active_mode() -> None:
    app, manager, _published = _plan_manager()
    active = app.active_mode()

    assert phase_mode(active, manager, None) is active
    assert phase_mode(active, manager, "") is active
    assert phase_mode(active, None, "Yolo") is active


def test_phase_mode_fails_closed_on_unknown_and_internal_overrides() -> None:
    app, manager, _published = _plan_manager()

    with pytest.raises(UnknownModeError):
        phase_mode(app.active_mode(), manager, "Turbo")
    internal = [
        mode
        for mode in manager.registry.all(include_internal=True)
        if manager.registry.is_internal(mode.name)
    ]
    assert internal, "the default registry registers internal modes such as Replay"
    for mode in internal:
        with pytest.raises(UnknownModeError):
            phase_mode(app.active_mode(), manager, mode.name)


def test_mode_manager_resolve_accepts_aliases_and_returns_the_canonical_mode() -> None:
    _app, manager, published = _plan_manager()

    assert manager.resolve("yolo").name == "Yolo"
    assert manager.resolve_name("yolo") == "Yolo"
    assert published == []


# ── declarative WorkflowRunner ────────────────────────────────────────────────


class _TwoPhase(WorkflowPlugin):
    name = "two_phase"
    description = "A phase that keeps the caller's mode and one that overrides it."
    phases = [
        PhaseSpec(name="inspect", next="write"),
        PhaseSpec(name="write", mode_override="Yolo"),
    ]


def _workflow_runner() -> tuple[WorkflowRunner, list[str]]:
    app, manager, published = _plan_manager()

    async def emit(_event: object) -> None:
        return None

    config = WorkflowConfig(
        conv_store=app.conversation,
        app_state=app,
        processor=SimpleNamespace(emit=emit),  # type: ignore[arg-type]
        agent_runner=SimpleNamespace(),  # type: ignore[arg-type]
        approval_svc=None,
        cfg=AgenthiccConfig(),
        skills={},
        plugin_tools=[stage_change, inspect_change],
        mcp_registry=None,
        mention_cache=SimpleNamespace(),  # type: ignore[arg-type]
        agents_registry=SimpleNamespace(),  # type: ignore[arg-type]
    )
    return WorkflowRunner(_TwoPhase, config, manager), published


def test_declarative_phase_tools_follow_the_phase_mode_override() -> None:
    runner, published = _workflow_runner()
    inspect_spec, write_spec = _TwoPhase.phases

    inspect_names = {tool_name(item) for item in runner._filter_tools(inspect_spec)}
    write_names = {tool_name(item) for item in runner._filter_tools(write_spec)}

    assert "inspect_change" in inspect_names
    assert "stage_change" not in inspect_names  # Plan blocks writes
    assert {"inspect_change", "stage_change"} <= write_names  # Yolo does not
    assert published == []


def test_declarative_phase_with_an_unknown_mode_override_fails_closed() -> None:
    runner, _published = _workflow_runner()

    with pytest.raises(UnknownModeError):
        runner._filter_tools(replace(_TwoPhase.phases[1], mode_override="Turbo"))


# ── override_builtin_tools ────────────────────────────────────────────────────


@tool_write
@tool()
async def write_file(path: str, content: str) -> dict[str, object]:
    """Stand-in for the built-in write_file (the registry keys tools by name)."""
    return {"ok": True, "replayed": True, "path": path, "bytes": len(content)}


_REPLAY_GROUP = ToolGroup(
    name="fs",
    label="File System",
    description="Replay stand-ins.",
    tools=[write_file],
    priority=4,
)


def test_override_replaces_the_builtin_catalog() -> None:
    real = build_registry(agent_name=None, project_plugin_tools=[])
    assert "run_bash" in real.names

    with override_builtin_tools([_REPLAY_GROUP]):
        replaced = build_registry(agent_name="auto", project_plugin_tools=[inspect_change])

    assert sorted(replaced.names) == ["inspect_change", "write_file"]
    assert replaced.tools[replaced.names.index("write_file")] is write_file
    assert "run_bash" in build_registry(agent_name=None, project_plugin_tools=[]).names


def test_override_never_discovers_plugins_from_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(**_kwargs: object) -> object:
        raise AssertionError("an override must not load plugin code from disk")

    monkeypatch.setattr("agenthicc.plugins.discovery.discover_project_tools", forbidden)
    monkeypatch.setattr("agenthicc.plugins.discovery.discover_agent_tools", forbidden)

    with override_builtin_tools([_REPLAY_GROUP]):
        registry = build_registry(agent_name="auto", project_plugin_tools=None)

    assert registry.names == ["write_file"]


async def test_override_is_inherited_by_tasks_created_inside_it() -> None:
    proceed = asyncio.Event()

    async def build_later() -> list[str]:
        await proceed.wait()
        return build_registry(agent_name=None, project_plugin_tools=[]).names

    with override_builtin_tools([_REPLAY_GROUP]):
        task = asyncio.create_task(build_later())
    # The block has exited; the task still runs under the override it copied.
    proceed.set()

    assert await task == ["write_file"]
    assert "run_bash" in build_registry(agent_name=None, project_plugin_tools=[]).names
