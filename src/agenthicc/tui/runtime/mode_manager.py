"""ModeManager for the TUI reactive runtime (PRD-65 §1, PRD-75, PRD-87).

RuntimeMode is a frozen dataclass that is the single source of truth for the
active mode.  ModeManager writes AppState.active_mode directly — callers never
write it.

PRD-87: workflow_name replaced by default_workflow + workflows tuple.
Mode→workflow mapping is now derived from WorkflowRegistry.mode_bindings_map().
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
    approval_required:    frozenset[str] = field(default_factory=frozenset)
    default_workflow:     str | None     = None          # run on user submit
    workflows:            tuple[str,...] = ()             # all available in this mode


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


def build_default_registry(
    default_map:   dict[str, str]       | None = None,
    available_map: dict[str, list[str]] | None = None,
) -> ModeRegistry:
    """Build the runtime mode registry from the existing agenthicc.modes system.

    default_map   — {mode_name: workflow_name} for the default workflow per mode.
    available_map — {mode_name: [workflow_name, …]} for all available workflows.
    Both are derived from WorkflowRegistry.mode_default_map() and
    WorkflowRegistry.mode_available_map() in tui_session.py.
    """
    from agenthicc.tools.capabilities import ToolCapability  # noqa: PLC0415

    _RESTRICTED = frozenset({
        ToolCapability.WRITE,
        ToolCapability.GIT_WRITE,
        ToolCapability.EXECUTE,
        ToolCapability.NETWORK,
    })

    # Plan mode blocks write/execute/network tools so the plan phase agent
    # cannot bypass the approval gate.  The execute phase switches to Auto
    # mode (via PhaseSpec.mode_override) to restore full tool access.
    _BLOCKED: dict[str, frozenset] = {
        "Plan": _RESTRICTED,
        "Ask":  _RESTRICTED,
        "Safe": _RESTRICTED,
    }

    _APPROVAL: dict[str, frozenset] = {
        "Guard": _RESTRICTED,
    }

    _default   = default_map   or {}
    _available = available_map or {}

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
                approval_required=_APPROVAL.get(mode.name, frozenset()),
                default_workflow=_default.get(mode.name),
                workflows=tuple(_available.get(mode.name, [])),
            ))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not load modes from agenthicc.modes: %s", exc)
    if not reg.get("Auto"):
        reg.register(RuntimeMode(name="Auto", badge="⏵⏵", description="Automatic"))
    if not reg.get("Guard"):
        reg.register(RuntimeMode(
            name="Guard",
            badge="⏸⏸",
            description=(
                "All tools are allowed, but side-effecting tools "
                "require explicit approval before each execution."
            ),
            blocked_capabilities=frozenset(),
            approval_required=_RESTRICTED,
        ))
    return reg


class ModeManager:
    """Manages the active mode.  Writes AppState.active_mode on every transition."""

    def __init__(
        self,
        registry:      ModeRegistry | None         = None,
        app_state:     Any                          = None,
        default_map:   dict[str, str] | None        = None,
        available_map: dict[str, list[str]] | None  = None,
    ) -> None:
        self._registry  = registry or build_default_registry(
            default_map=default_map, available_map=available_map,
        )
        self._app_state = app_state
        self._idx       = 0
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
        modes = self._registry.all()
        if len(modes) > 1:
            self._idx = (self._idx + 1) % len(modes)
        new_mode = self.active
        if self._app_state is not None:
            self._app_state.active_mode.set(new_mode)
        return new_mode

    def set_by_name(self, name: str) -> RuntimeMode | None:
        for i, m in enumerate(self._registry.all()):
            if m.name == name:
                self._idx = i
                if self._app_state is not None:
                    self._app_state.active_mode.set(m)
                return m
        return None


def build_mode_str(mode: RuntimeMode) -> str:
    return f"{mode.badge} {mode.name}  (shift+tab to cycle){_NEW_LINE_HINT}"
