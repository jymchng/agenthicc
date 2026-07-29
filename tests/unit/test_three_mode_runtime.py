"""Clean-slate unit coverage for the PRD-155 canonical mode model."""

from __future__ import annotations

import pytest

from agenthicc.tools.capabilities import ToolCapability
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import (
    DEFAULT_MODE_NAME,
    ModeManager,
    ModeRegistry,
    RuntimeMode,
    UnknownModeError,
    build_default_registry,
    canonical_mode_name,
)

pytestmark = pytest.mark.unit


def test_default_registry_has_exact_selectable_order_and_internal_replay() -> None:
    registry = build_default_registry()

    assert [mode.name for mode in registry.all()] == ["Safe", "Plan", "Yolo"]
    assert [mode.name for mode in registry.all(include_internal=True)] == [
        "Safe",
        "Plan",
        "Yolo",
        "Replay",
    ]
    assert registry.get("Replay") is not None
    assert registry.is_internal("Replay") is True
    replay = registry.get("Replay")
    assert replay is not None
    assert ToolCapability.CONTROL in replay.blocked_capabilities


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [("Auto", "Yolo"), ("GUARD", "Safe"), ("ask", "Safe"), ("Review", "Plan")],
)
def test_legacy_aliases_are_case_insensitive_and_not_selectable(
    legacy: str, canonical: str
) -> None:
    registry = build_default_registry()
    assert registry.resolve(legacy).name == canonical
    assert registry.get(legacy).name == canonical  # type: ignore[union-attr]
    assert legacy.casefold() not in {name.casefold() for name in registry.selectable_names()}


def test_unknown_mode_is_actionable_and_never_falls_back() -> None:
    registry = build_default_registry()

    with pytest.raises(UnknownModeError, match="Safe, Plan, Yolo"):
        registry.resolve("Debug")
    assert registry.get("Debug") is None

    with pytest.raises(UnknownModeError, match="Safe, Plan, Yolo"):
        ModeManager(registry, default_name="Debug")


def test_registry_rejects_duplicate_names_and_alias_collisions() -> None:
    registry = ModeRegistry({"old": "Safe"})
    registry.register(RuntimeMode("Safe"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(RuntimeMode("safe"))
    with pytest.raises(ValueError, match="collides"):
        registry.register(RuntimeMode("old"))


def test_default_map_migrates_auto_to_yolo() -> None:
    registry = build_default_registry(
        default_map={"Auto": "legacy_workflow", "Plan": "code_plan"},
        available_map={"Auto": ["legacy_workflow"]},
    )

    assert registry.get("Yolo").default_workflow == "legacy_workflow"  # type: ignore[union-attr]
    assert registry.get("Auto").name == "Yolo"  # type: ignore[union-attr]
    assert registry.get("Plan").default_workflow == "code_plan"  # type: ignore[union-attr]


def test_workflow_mode_names_are_canonicalized_case_insensitively() -> None:
    assert canonical_mode_name(" safe ") == "Safe"
    assert canonical_mode_name("yolo") == "Yolo"
    assert canonical_mode_name("AUTO") == "Yolo"


def test_mode_manager_defaults_safe_cycles_only_selectable_modes_and_accepts_aliases() -> None:
    app = AppState.create()
    manager = ModeManager(build_default_registry(), app)

    assert DEFAULT_MODE_NAME == "Safe"
    assert manager.active_name == "Safe"
    assert app.active_mode().name == "Safe"
    assert [manager.cycle().name for _ in range(4)] == ["Plan", "Yolo", "Safe", "Plan"]
    assert manager.set_by_name("auto").name == "Yolo"  # type: ignore[union-attr]
    assert manager.set_by_name("DEBUG") is None
    assert manager.active_name == "Yolo"


def test_replay_is_trusted_internal_state_and_restore_keeps_manager_in_sync() -> None:
    app = AppState.create()
    manager = ModeManager(build_default_registry(), app)
    prior = manager.set_by_name("Yolo")
    assert prior is not None

    replay = manager.set_internal_by_name("Replay")
    assert replay is not None
    assert app.active_mode().name == "Replay"
    assert manager.set_by_name("Replay") is None
    manager.restore(prior)
    assert manager.active_name == "Yolo"
    assert app.active_mode().name == "Yolo"


def test_mode_change_callback_receives_canonical_names_only() -> None:
    changes: list[str] = []
    manager = ModeManager(
        build_default_registry(), on_change=lambda mode: changes.append(mode.name)
    )

    manager.set_by_name("Auto")
    manager.cycle()
    manager.set_internal_by_name("Replay")
    manager.set_by_name("Safe")

    assert changes == ["Yolo", "Safe", "Replay", "Safe"]


def test_default_runtime_policy_matches_prd_capability_matrix() -> None:
    registry = build_default_registry()
    side_effects = {
        ToolCapability.WRITE,
        ToolCapability.GIT_WRITE,
        ToolCapability.EXECUTE,
        ToolCapability.NETWORK,
    }
    safe = registry.get("Safe")
    plan = registry.get("Plan")
    yolo = registry.get("Yolo")
    assert safe is not None and plan is not None and yolo is not None

    assert side_effects <= safe.approval_required
    assert side_effects <= plan.blocked_capabilities
    assert not yolo.blocked_capabilities
    assert not yolo.approval_required
    assert ToolCapability.READ not in safe.approval_required
    assert ToolCapability.SEARCH not in safe.approval_required
    assert ToolCapability.GIT_READ not in safe.approval_required
    assert ToolCapability.CONTROL not in plan.blocked_capabilities


def test_app_state_default_is_the_full_safe_policy_before_session_wiring() -> None:
    app = AppState.create()
    safe = app.active_mode()

    assert safe.name == "Safe"
    assert ToolCapability.WRITE in safe.approval_required
    assert ToolCapability.UNDECLARED in safe.approval_required
    assert ToolCapability.READ not in safe.approval_required


def test_runtime_manager_can_restore_a_plugin_mode_without_aliasing_it() -> None:
    registry = ModeRegistry()
    registry.register(RuntimeMode("Safe"))
    registry.register(RuntimeMode("Plan"))
    registry.register(RuntimeMode("Yolo"))
    registry.register(RuntimeMode("Acme"))
    manager = ModeManager(registry, default_name="Acme")

    assert manager.active_name == "Acme"
    assert manager.set_by_name("acme").name == "Acme"  # type: ignore[union-attr]
    with pytest.raises(UnknownModeError):
        registry.resolve("Auto")


def test_legacy_plugin_adapter_preserves_custom_binding_and_rejects_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from agenthicc.modes.mode import Mode
    from agenthicc.modes import plugin_loader

    monkeypatch.setattr(
        plugin_loader,
        "discover_mode_plugins",
        lambda: SimpleNamespace(
            all_modes=[
                Mode("Acme", "ACME", "Custom mode"),
                Mode("Debug", "DEBUG", "Rejected mode"),
            ],
            failed=[],
        ),
    )
    registry = build_default_registry(
        default_map={"Acme": "custom_workflow"},
        available_map={"Acme": ["custom_workflow", "custom_workflow"]},
    )

    acme = registry.get("Acme")
    assert acme is not None
    assert acme.default_workflow == "custom_workflow"
    assert acme.workflows == ("custom_workflow",)
    assert registry.get("Debug") is None


def test_write_capable_builtin_phases_declare_yolo_and_not_auto() -> None:
    from agenthicc.workflows.code_plan.definition import CodePlan
    from agenthicc.workflows.create_workflow.definition import CreateWorkflow

    code_plan = {phase.name: phase.mode_override for phase in CodePlan.phases}
    create_workflow = {phase.name: phase.mode_override for phase in CreateWorkflow.phases}

    assert code_plan["execute"] == "Yolo"
    assert create_workflow["generate"] == "Yolo"
    assert "Auto" not in {value for value in (*code_plan.values(), *create_workflow.values())}
