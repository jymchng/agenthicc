"""ModeManager for the TUI reactive runtime (PRD-65 §1).

``RuntimeMode`` is a thin wrapper that maps to the existing ``agenthicc.modes``
Mode dataclass.  ModeRegistry wraps the existing ModeRegistry.  This avoids
duplicating the mode data model while giving the runtime its own clean API.
"""
from __future__ import annotations

from dataclasses import dataclass


_NEW_LINE_HINT = "  │  ctrl+j = ↵"


@dataclass(frozen=True)
class RuntimeMode:
    """A named execution context for the agent."""
    name:                str
    badge:               str   = "⏵⏵"
    description:         str   = ""
    system_prompt_suffix:str   = ""


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
    """Build the default mode registry, importing from the existing modes system."""
    reg = ModeRegistry()
    # Mirror from existing agenthicc.modes if available
    try:
        from agenthicc.modes import build_default_registry as _bdr   # noqa: PLC0415
        from agenthicc.modes.manager import ModeManager as _MM       # noqa: PLC0415
        existing_reg = _bdr()
        existing_mm  = _MM(existing_reg)
        for mode in existing_mm._registry.all() if hasattr(existing_mm, "_registry") else []:
            reg.register(RuntimeMode(
                name=getattr(mode, "name", "Unknown"),
                badge=getattr(mode, "label", "??"),
                description=getattr(mode, "description", ""),
                system_prompt_suffix=getattr(mode, "system_patch", ""),
            ))
    except Exception:
        pass
    # Always ensure Auto exists
    if not reg.get("Auto"):
        reg.register(RuntimeMode(name="Auto", badge="⏵⏵", description="Automatic"))
    return reg


class ModeManager:
    """Manages the active mode and cycling (Shift+Tab)."""

    def __init__(self, registry: ModeRegistry | None = None) -> None:
        self._registry = registry or build_default_registry()
        self._idx = 0

    @property
    def active(self) -> RuntimeMode:
        modes = self._registry.all()
        if not modes:
            return RuntimeMode(name="Auto", badge="⏵⏵")
        return modes[self._idx % len(modes)]

    def cycle(self) -> RuntimeMode:
        """Advance to the next mode and return it."""
        modes = self._registry.all()
        if len(modes) > 1:
            self._idx = (self._idx + 1) % len(modes)
        return self.active

    def set_by_name(self, name: str) -> RuntimeMode | None:
        for i, m in enumerate(self._registry.all()):
            if m.name == name:
                self._idx = i
                return m
        return None


def build_mode_str(mode: RuntimeMode) -> str:
    """Return the footer mode string for a given mode."""
    if mode.name == "Auto":
        return f"{mode.badge} Auto  (shift+tab to cycle){_NEW_LINE_HINT}"
    return f"{mode.badge} {mode.name}  (shift+tab to cycle){_NEW_LINE_HINT}"
