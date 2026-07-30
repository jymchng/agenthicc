"""Additional malformed-registry and fallback coverage for runtime modes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agenthicc.tui.runtime.mode_manager import (
    ModeManager,
    ModeRegistry,
    RuntimeMode,
    UnknownModeError,
    build_default_registry,
    build_mode_str,
)

pytestmark = pytest.mark.unit


def test_mode_registry_alias_and_internal_boundaries() -> None:
    registry = ModeRegistry()
    registry.register(RuntimeMode("Safe"))
    registry.register(RuntimeMode("Replay"), selectable=False)
    with pytest.raises(ValueError, match="collides"):
        registry.register_alias("safe", "Safe")
    registry.register_alias("old", "Safe")
    assert registry.aliases() == {"old": "Safe"}
    with pytest.raises(ValueError, match="already targets"):
        registry.register_alias("old", "Replay")
    with pytest.raises(ValueError, match="collides"):
        registry.register_alias("Replay", "Safe")
    assert registry.all() == [registry.get("Safe")]
    assert [mode.name for mode in registry.all(include_internal=True)] == ["Safe", "Replay"]
    assert registry.get("Replay", include_internal=False) is None
    assert registry.is_internal("Replay") is True
    assert registry.is_internal("missing") is False
    with pytest.raises(UnknownModeError):
        registry.resolve("")


def test_mode_manager_empty_registry_fallback_and_callbacks() -> None:
    registry = ModeRegistry()
    manager = ModeManager(registry)
    assert manager.active.name == "Safe"
    assert manager.cycle().name == "Safe"
    assert manager.set_internal_by_name("missing") is None
    with pytest.raises(UnknownModeError):
        manager.restore(RuntimeMode("missing"))
    assert build_mode_str(RuntimeMode("Demo", badge="D", color="blue")).startswith("[blue]D Demo")


def test_legacy_mode_discovery_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agenthicc.modes.plugin_loader.discover_mode_plugins",
        lambda: (_ for _ in ()).throw(RuntimeError("broken plugin loader")),
    )
    registry = build_default_registry()
    assert [mode.name for mode in registry.all()] == ["Safe", "Plan", "Yolo"]


def test_legacy_invalid_plugin_is_ignored_and_failures_are_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.modes.mode import Mode

    monkeypatch.setattr(
        "agenthicc.modes.plugin_loader.discover_mode_plugins",
        lambda: SimpleNamespace(
            all_modes=[
                Mode("Broken", "B", "ok"),
                Mode("Auto", "A", "alias"),
                Mode("Safe", "S", "duplicate"),
                Mode("", "bad", "invalid"),
            ],
            failed=[SimpleNamespace(path="plugin.py", error="failed")],
        ),
    )
    registry = build_default_registry()
    assert registry.get("Broken") is not None
    assert registry.get("") is None


def test_mode_manager_rejects_internal_selection_and_recovers_cycle_index() -> None:
    registry = build_default_registry()
    with pytest.raises(UnknownModeError):
        ModeManager(registry, default_name="Replay")
    manager = ModeManager(registry)
    manager._registry.get = lambda _name: RuntimeMode("Ghost")  # type: ignore[method-assign]
    assert manager.cycle().name == "Ghost"
    replay = manager.set_internal_by_name("Replay")
    assert replay is not None
    assert manager.restore(replay).name == "Replay"
    with pytest.raises(UnknownModeError):
        manager.resolve_name("Replay")
