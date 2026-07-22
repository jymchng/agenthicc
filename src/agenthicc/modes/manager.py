"""ModeManager — tracks the currently active Mode and applies it to agent calls."""

from __future__ import annotations

from .mode import Mode
from .registry import ModeRegistry

__all__ = ["ModeManager"]


class ModeManager:
    """Manages the currently active :class:`Mode` for a session.

    The manager wraps a :class:`ModeRegistry` and keeps track of which mode is
    active.  It exposes helpers for:

    * Cycling to the next mode (e.g. via a keyboard shortcut).
    * Setting a mode by name directly.
    * Applying the active mode's system-prompt patch and tool filter to the
      agent configuration before each invocation.

    Parameters
    ----------
    registry:
        The :class:`ModeRegistry` from which modes are resolved.
    default_name:
        Name of the mode to activate initially.  Falls back to the first
        registered mode if *default_name* is not found.  Defaults to
        ``"Auto"``.

    Examples
    --------
    >>> from agenthicc.modes.builtins import build_default_registry
    >>> manager = ModeManager(build_default_registry())
    >>> manager.active_name
    'Auto'
    >>> manager.cycle().name in ['Plan', 'Ask', 'Review', 'Safe', 'Debug']
    True
    """

    def __init__(self, registry: ModeRegistry, default_name: str = "Auto") -> None:
        self._registry = registry
        default = registry.get(default_name) or (
            registry.all_modes()[0] if registry.all_modes() else None
        )
        self._active: Mode | None = default

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def active(self) -> Mode | None:
        """The currently active :class:`Mode`, or ``None`` when the registry is empty."""
        return self._active

    @property
    def active_name(self) -> str:
        """The name of the active mode; ``"Auto"`` when nothing is active."""
        return self._active.name if self._active is not None else "Auto"

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def cycle(self) -> Mode:
        """Advance to the next mode in registry order and return it.

        Raises
        ------
        ValueError
            When the registry is empty.
        """
        self._active = self._registry.next_after(self.active_name)
        return self._active

    def set(self, name: str) -> Mode | None:
        """Activate the mode named *name* and return it.

        Returns ``None`` (and leaves the current mode unchanged) when *name*
        is not found in the registry.
        """
        mode = self._registry.get(name)
        if mode is not None:
            self._active = mode
        return mode

    # ------------------------------------------------------------------
    # Agent integration
    # ------------------------------------------------------------------

    def apply_to_agent(
        self,
        base_system: str,
        registry_tools: list[object],
    ) -> tuple[str, list[object]]:
        """Return a (system_prompt, tools) pair shaped by the active mode.

        Transformations applied:

        * If the mode has a ``system_patch``, it is prepended to
          *base_system* (separated by a blank line).
        * If the mode has a ``tool_filter``, only tools whose
          ``__name__`` (or ``str()`` representation) passes the filter
          are included.

        Parameters
        ----------
        base_system:
            The unmodified system prompt string.
        registry_tools:
            The full list of tool objects available to the agent.

        Returns
        -------
        tuple[str, list]
            ``(system_prompt, tools)`` ready to pass to the agent runner.
        """
        mode = self._active
        if mode is None:
            return base_system, registry_tools

        system = base_system
        if mode.system_patch:
            system = mode.system_patch.rstrip() + "\n\n" + system

        tools = registry_tools
        if mode.tool_filter is not None:
            tools = [
                t for t in registry_tools if mode.tool_filter(getattr(t, "__name__", str(t)), {})
            ]

        return system, tools

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover
        return f"ModeManager(active={self.active_name!r})"
