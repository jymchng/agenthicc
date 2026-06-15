"""ModeManager for the TUI reactive runtime (PRD-65 §1, PRD-75).

RuntimeMode is a frozen dataclass that is the single source of truth for the
active mode.  ModeManager writes AppState.active_mode directly — callers never
write it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_NEW_LINE_HINT = "  │  ctrl+j = ↵"


@dataclass(frozen=True)
class RuntimeMode:
    """A named execution context for the agent."""
    name:                 str
    badge:                str            = "⏵⏵"
    description:          str            = ""
    system_prompt_suffix: str            = ""
    blocked_capabilities: frozenset[str] = field(default_factory=frozenset)


class ModeRegistry:
    """Ordered list of RuntimeModes available in this session."""

    def __init__(self) -> None:
        self._modes: list[RuntimeMode] = []

    def register(self, mode: RuntimeMode) -> None:
        self._modes.append(mode)

    def all(self) -> list[RuntimeMode]:
        return list(self._modes)

    def get(self, name: str) -> RuntimeMode | None:
        return next((m for m in self._modes if m.name == name), None)


def build_default_registry() -> ModeRegistry:
    """Build the runtime mode registry from the existing agenthicc.modes system."""
    from agenthicc.tools.capabilities import ToolCapability  # noqa: PLC0415

    # Capabilities blocked by all restrictive modes (Plan / Ask / Review / Safe).
    _RESTRICTED = frozenset({
        ToolCapability.WRITE,
        ToolCapability.GIT_WRITE,
        ToolCapability.EXECUTE,
        ToolCapability.NETWORK,
    })

    # Per-mode blocked capabilities; absent key → no blocking (open access).
    _BLOCKED: dict[str, frozenset] = {
        "Plan":   _RESTRICTED,
        "Ask":    _RESTRICTED,
        "Review": _RESTRICTED,
        "Safe":   _RESTRICTED,
    }

    reg = ModeRegistry()
    try:
        from agenthicc.modes.builtin import build_default_registry as _bdr  # noqa: PLC0415
        from agenthicc.modes.manager import ModeManager as _MM              # noqa: PLC0415
        existing_mm = _MM(_bdr())
        for mode in existing_mm._registry.all_modes():
            reg.register(RuntimeMode(
                name=mode.name,
                badge=getattr(mode, "label", mode.name),
                description=getattr(mode, "description", ""),
                system_prompt_suffix=getattr(mode, "system_patch", ""),
                blocked_capabilities=_BLOCKED.get(mode.name, frozenset()),
            ))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not load modes from agenthicc.modes: %s", exc)
    if not reg.get("Auto"):
        reg.register(RuntimeMode(name="Auto", badge="⏵⏵", description="Automatic"))
    return reg


class ModeManager:
    """Manages the active mode.  Writes AppState.active_mode on every transition.

    All signal writes are centralised here — callers only call cycle() or
    set_by_name() and never touch the signal directly.
    """

    def __init__(self, registry: ModeRegistry | None = None, app_state: Any = None) -> None:
        self._registry  = registry or build_default_registry()
        self._app_state = app_state
        self._idx       = 0
        # Sync the signal with the initial active mode
        if app_state is not None:
            app_state.active_mode.set(self.active)

    @property
    def active(self) -> RuntimeMode:
        modes = self._registry.all()
        if not modes:
            return RuntimeMode(name="Auto", badge="⏵⏵")
        return modes[self._idx % len(modes)]

    @property
    def active_name(self) -> str:
        return self.active.name

    def cycle(self) -> RuntimeMode:
        """Advance to the next mode and update AppState.active_mode."""
        modes = self._registry.all()
        if len(modes) > 1:
            self._idx = (self._idx + 1) % len(modes)
        new_mode = self.active
        if self._app_state is not None:
            self._app_state.active_mode.set(new_mode)
        return new_mode

    def set_by_name(self, name: str) -> RuntimeMode | None:
        """Activate the named mode and update AppState.active_mode."""
        for i, m in enumerate(self._registry.all()):
            if m.name == name:
                self._idx = i
                if self._app_state is not None:
                    self._app_state.active_mode.set(m)
                return m
        return None


def build_mode_str(mode: RuntimeMode) -> str:
    """Return the footer mode string for a given mode."""
    return f"{mode.badge} {mode.name}  (shift+tab to cycle){_NEW_LINE_HINT}"
