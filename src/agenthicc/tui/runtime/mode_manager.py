"""Canonical runtime modes for interactive and headless sessions.

The runtime registry is the source of truth for mode identity and policy.  The
older :mod:`agenthicc.modes` package remains a plugin compatibility boundary,
but runtime code no longer builds a second registry and reaches through its
private fields.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from collections.abc import Callable
from typing import Protocol

from agenthicc.reactive import Signal

log = logging.getLogger(__name__)

DEFAULT_MODE_NAME = "Safe"
SELECTABLE_MODE_NAMES = ("Safe", "Plan", "Yolo")
INTERNAL_MODE_NAMES = ("Replay",)

# Compatibility is deliberately resolved at the registry boundary.  Aliases
# never appear in ``all()`` and therefore cannot become duplicate cycle entries.
MODE_ALIASES: dict[str, str] = {
    "auto": "Yolo",
    "guard": "Safe",
    "ask": "Safe",
    "review": "Plan",
}
_CANONICAL_NAMES = {name.casefold(): name for name in SELECTABLE_MODE_NAMES + INTERNAL_MODE_NAMES}


class UnknownModeError(ValueError):
    """Raised when a mode name is neither canonical nor a known alias."""

    def __init__(self, name: str, valid_names: tuple[str, ...]) -> None:
        self.name = name
        self.valid_names = valid_names
        choices = ", ".join(valid_names)
        super().__init__(f"Unknown mode {name!r}. Choose one of: {choices}.")


class _ModeState(Protocol):
    active_mode: Signal["RuntimeMode"]


@dataclass(frozen=True)
class RuntimeMode:
    """A named execution context for the agent."""

    name: str
    badge: str = "⏵⏵"
    color: str = "white"  # Rich color for badge + name
    description: str = ""
    system_prompt_suffix: str = ""
    blocked_capabilities: frozenset[str] = field(default_factory=frozenset)
    approval_required: frozenset[str] = field(default_factory=frozenset)
    default_workflow: str | None = None  # run on user submit
    workflows: tuple[str, ...] = ()  # all available in this mode


class ModeRegistry:
    """Ordered canonical runtime modes with aliases and internal entries.

    ``all()`` returns only user-selectable entries.  Internal modes such as
    Replay can be registered with ``selectable=False`` and resolved by trusted
    runtime code without appearing in cycling or ``/mode`` output.
    """

    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self._modes: dict[str, RuntimeMode] = {}
        self._order: list[str] = []
        self._internal: set[str] = set()
        self._aliases: dict[str, str] = {}
        for alias, target in (aliases or {}).items():
            self._aliases[_normalize(alias)] = target

    def register(self, mode: RuntimeMode, *, selectable: bool = True) -> None:
        """Register a canonical mode, rejecting duplicate identities."""
        name = _validate_name(mode.name)
        key = _normalize(name)
        if key in self._aliases:
            raise ValueError(f"Mode name {name!r} collides with an existing alias.")
        if key in self._modes:
            raise ValueError(f"Mode {name!r} is already registered.")
        self._modes[key] = mode
        if selectable:
            self._order.append(key)
        else:
            self._internal.add(key)

    def register_alias(self, alias: str, target: str) -> None:
        """Register a non-selectable compatibility alias for *target*."""
        alias_key = _normalize(_validate_name(alias))
        target_mode = self.resolve(target)
        if alias_key in self._modes:
            raise ValueError(f"Alias {alias!r} collides with a registered mode.")
        existing = self._aliases.get(alias_key)
        if existing is not None and existing != target_mode.name:
            raise ValueError(f"Alias {alias!r} already targets {existing!r}.")
        self._aliases[alias_key] = target_mode.name

    def all(self, *, include_internal: bool = False) -> list[RuntimeMode]:
        """Return modes in cycle order, optionally followed by internals."""
        names = list(self._order)
        if include_internal:
            names.extend(key for key in self._modes if key in self._internal)
        return [self._modes[key] for key in names]

    def get(self, name: str, *, include_internal: bool = True) -> RuntimeMode | None:
        """Resolve a canonical name or alias without raising for unknown input."""
        try:
            mode = self.resolve(name)
        except UnknownModeError:
            return None
        if not include_internal and _normalize(mode.name) in self._internal:
            return None
        return mode

    def resolve(self, name: str) -> RuntimeMode:
        """Resolve a canonical name or alias, raising an actionable error."""
        if not isinstance(name, str) or not name.strip():
            raise UnknownModeError(str(name), self.selectable_names())
        key = _normalize(name)
        canonical = self._aliases.get(key, key)
        mode = self._modes.get(_normalize(canonical))
        if mode is None:
            raise UnknownModeError(name, self.selectable_names())
        return mode

    def aliases(self) -> dict[str, str]:
        """Return a snapshot of compatibility aliases keyed by lower-case name."""
        return dict(self._aliases)

    def is_alias(self, name: str) -> bool:
        """Return whether *name* is a registered compatibility spelling."""
        return _normalize(name) in self._aliases

    def selectable_names(self) -> tuple[str, ...]:
        """Return canonical names accepted for normal user selection."""
        return tuple(self._modes[key].name for key in self._order)

    def is_internal(self, name: str) -> bool:
        """Return whether *name* resolves to an internal-only mode."""
        try:
            return _normalize(self.resolve(name).name) in self._internal
        except UnknownModeError:
            return False

    def refresh_workflows(
        self,
        default_map: dict[str, str],
        available_map: dict[str, list[str]],
    ) -> None:
        """Refresh workflow bindings after deferred workflow discovery."""
        defaults = _canonical_workflow_map(default_map)
        available = _canonical_available_map(available_map)
        for key, mode in tuple(self._modes.items()):
            if key in self._internal:
                continue
            self._modes[key] = replace(
                mode,
                default_workflow=defaults.get(mode.name),
                workflows=tuple(available.get(mode.name, ())),
            )


def _normalize(name: str) -> str:
    return name.strip().casefold()


def canonical_mode_name(name: str) -> str:
    """Return the canonical spelling for a mode or compatibility alias."""
    key = _normalize(name)
    return MODE_ALIASES.get(key, _CANONICAL_NAMES.get(key, name.strip()))


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Mode names must be non-empty strings.")
    return name.strip()


def _restricted_capabilities() -> frozenset[str]:
    from agenthicc.tools.capabilities import ToolCapability  # noqa: PLC0415

    return frozenset(
        {
            ToolCapability.WRITE,
            ToolCapability.GIT_WRITE,
            ToolCapability.EXECUTE,
            ToolCapability.NETWORK,
            ToolCapability.UNDECLARED,
        }
    )


def build_safe_mode(
    *,
    default_workflow: str | None = None,
    workflows: tuple[str, ...] = (),
) -> RuntimeMode:
    """Return the canonical Safe policy for any default-state boundary."""
    return RuntimeMode(
        name="Safe",
        badge="⊘",
        color="red",
        description="Side-effecting actions require explicit approval; reads are allowed.",
        system_prompt_suffix=(
            "## SAFE MODE\n"
            "Side-effecting tools require explicit user approval before execution. "
            "Read, search, and git-read tools may be used directly."
        ),
        approval_required=_restricted_capabilities(),
        default_workflow=default_workflow,
        workflows=workflows,
    )


def build_default_registry(
    default_map: dict[str, str] | None = None,
    available_map: dict[str, list[str]] | None = None,
) -> ModeRegistry:
    """Build the canonical Safe → Plan → Yolo runtime registry.

    Legacy mode plugins are adapted after the three built-ins.  Their names
    remain extension identities, but their tools still pass through the same
    capability and approval gates.  Legacy built-in names are aliases and do
    not create additional selectable modes.
    """
    from agenthicc.tools.capabilities import ToolCapability  # noqa: PLC0415

    restricted = _restricted_capabilities()
    defaults = _canonical_workflow_map(default_map or {})
    available = _canonical_available_map(available_map or {})

    registry = ModeRegistry(MODE_ALIASES)
    registry.register(
        build_safe_mode(
            default_workflow=defaults.get("Safe"),
            workflows=tuple(available.get("Safe", [])),
        )
    )
    registry.register(
        RuntimeMode(
            name="Plan",
            badge="◈",
            color="yellow",
            description="Read-only planning; side-effecting actions are hard-blocked.",
            system_prompt_suffix=(
                "## PLAN MODE\n"
                "You are operating in PLAN MODE. You MUST NOT write files, execute "
                "commands, or make changes to the filesystem or repository. Analyse "
                "the request and produce a structured action plan."
            ),
            blocked_capabilities=restricted,
            default_workflow=defaults.get("Plan"),
            workflows=tuple(available.get("Plan", [])),
        )
    )
    registry.register(
        RuntimeMode(
            name="Yolo",
            badge="⏵⏵",
            color="green",
            description="Full automatic mode; all tools are allowed without prompts.",
            default_workflow=defaults.get("Yolo"),
            workflows=tuple(available.get("Yolo", [])),
        )
    )

    # Replay is a trusted internal state, not a user-selectable permission
    # profile. It is intentionally absent from the cycle and /mode listing.
    all_capabilities = frozenset(
        {
            ToolCapability.READ,
            ToolCapability.SEARCH,
            ToolCapability.GIT_READ,
            ToolCapability.WRITE,
            ToolCapability.GIT_WRITE,
            ToolCapability.EXECUTE,
            ToolCapability.NETWORK,
            ToolCapability.CONTROL,
            ToolCapability.UNDECLARED,
        }
    )
    registry.register(
        RuntimeMode(
            name="Replay",
            badge="⏮",
            color="dim",
            description="Internal replay state; all tool capabilities are blocked.",
            blocked_capabilities=all_capabilities,
        ),
        selectable=False,
    )

    _load_legacy_plugins(registry, defaults, available)
    return registry


def _canonical_workflow_map(values: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, workflow in values.items():
        canonical = canonical_mode_name(name)
        # Preserve names belonging to downstream mode plugins while migrating
        # built-in aliases. Replay and Debug are runtime-reserved identities,
        # never valid workflow bindings.
        if canonical.casefold() not in {"replay", "debug"} and canonical not in result:
            result[canonical] = workflow
    return result


def _canonical_available_map(values: dict[str, list[str]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for name, workflows in values.items():
        canonical = canonical_mode_name(name)
        if canonical.casefold() not in {"replay", "debug"}:
            entries = result.setdefault(canonical, [])
            for workflow in workflows:
                if workflow not in entries:
                    entries.append(workflow)
    return result


def _load_legacy_plugins(
    registry: ModeRegistry,
    defaults: dict[str, str],
    available: dict[str, list[str]],
) -> None:
    """Adapt user mode plugins without consulting the legacy registry internals."""
    conservative_policy = _restricted_capabilities()
    try:
        from agenthicc.modes.plugin_loader import discover_mode_plugins  # noqa: PLC0415

        plugins = discover_mode_plugins()
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not load mode plugins: %s", exc)
        return

    for legacy in plugins.all_modes:
        if _normalize(legacy.name) == "debug":
            log.warning("Ignoring mode plugin %r: Debug is a rejected mode name", legacy.name)
            continue
        if registry.is_alias(legacy.name):
            log.warning(
                "Ignoring mode plugin %r: name is a reserved compatibility alias", legacy.name
            )
            continue
        if registry.get(legacy.name) is not None:
            log.warning("Ignoring duplicate mode plugin %r", legacy.name)
            continue
        try:
            registry.register(
                RuntimeMode(
                    name=legacy.name,
                    badge=getattr(legacy, "label", legacy.name),
                    color=getattr(legacy, "colour", "white"),
                    description=getattr(legacy, "description", ""),
                    system_prompt_suffix=getattr(legacy, "system_patch", ""),
                    default_workflow=defaults.get(legacy.name),
                    workflows=tuple(available.get(legacy.name, [])),
                    # A legacy plugin has no capability-policy contract.  Keep
                    # it conservative rather than allowing it to weaken the
                    # canonical gate; plugin authors can select Yolo explicitly
                    # when unrestricted execution is intended.
                    approval_required=conservative_policy,
                )
            )
        except (TypeError, ValueError) as exc:
            log.warning("Ignoring invalid mode plugin %r: %s", legacy.name, exc)
    for failed in plugins.failed:
        log.warning("Failed to load mode plugin %s: %s", failed.path, failed.error)


class ModeManager:
    """Manage active canonical mode and write it to AppState when attached."""

    def __init__(
        self,
        registry: ModeRegistry | None = None,
        app_state: _ModeState | None = None,
        default_map: dict[str, str] | None = None,
        available_map: dict[str, list[str]] | None = None,
        default_name: str = DEFAULT_MODE_NAME,
        on_change: Callable[[RuntimeMode], None] | None = None,
    ) -> None:
        self._registry = registry or build_default_registry(
            default_map=default_map,
            available_map=available_map,
        )
        self._app_state = app_state
        self._on_change = on_change
        self._active_name = self._initial_name(default_name)
        if app_state is not None:
            app_state.active_mode.set(self.active)

    def _initial_name(self, requested: str) -> str:
        modes = self._registry.all()
        try:
            mode = self._registry.resolve(requested)
        except UnknownModeError:
            # A compatibility registry supplied by an embedding caller may
            # omit Safe; only the implicit Safe default may use its first mode
            # as a fallback. Explicit unknown selections must fail closed.
            if _normalize(requested) == _normalize(DEFAULT_MODE_NAME):
                return modes[0].name if modes else DEFAULT_MODE_NAME
            raise
        if self._registry.is_internal(mode.name):
            raise UnknownModeError(requested, self._registry.selectable_names())
        return mode.name

    @property
    def registry(self) -> ModeRegistry:
        """Return the session registry for trusted callers and renderers."""
        return self._registry

    @property
    def active(self) -> RuntimeMode:
        mode = self._registry.get(self._active_name)
        if mode is not None:
            return mode
        modes = self._registry.all()
        return modes[0] if modes else RuntimeMode(name=DEFAULT_MODE_NAME)

    @property
    def active_name(self) -> str:
        return self.active.name

    def refresh_workflow_bindings(
        self,
        default_map: dict[str, str],
        available_map: dict[str, list[str]],
    ) -> None:
        """Apply workflow bindings discovered after the initial TUI frame."""
        self._registry.refresh_workflows(default_map, available_map)
        self._publish(self.active)

    def cycle(self) -> RuntimeMode:
        modes = self._registry.all()
        if modes:
            names = [mode.name for mode in modes]
            try:
                index = names.index(self.active.name)
            except ValueError:
                index = -1
            self._active_name = names[(index + 1) % len(names)]
        new_mode = self.active
        self._publish(new_mode)
        return new_mode

    def set_by_name(self, name: str) -> RuntimeMode | None:
        """Select a canonical name or alias; return ``None`` for unknown input."""
        try:
            mode = self._registry.resolve(name)
        except UnknownModeError:
            return None
        if self._registry.is_internal(mode.name):
            return None
        self._active_name = mode.name
        self._publish(mode)
        return mode

    def set_internal_by_name(self, name: str) -> RuntimeMode | None:
        """Select an internal mode for trusted lifecycle operations only."""
        try:
            mode = self._registry.resolve(name)
        except UnknownModeError:
            return None
        if not self._registry.is_internal(mode.name):
            return self.set_by_name(mode.name)
        self._active_name = mode.name
        self._publish(mode)
        return mode

    def restore(self, mode: RuntimeMode) -> RuntimeMode:
        """Restore a previous mode while keeping manager state coherent."""
        if self._registry.is_internal(mode.name):
            restored = self.set_internal_by_name(mode.name)
        else:
            restored = self.set_by_name(mode.name)
        if restored is None:
            raise UnknownModeError(mode.name, self._registry.selectable_names())
        return restored

    def set_change_callback(self, callback: Callable[[RuntimeMode], None] | None) -> None:
        """Set the optional persistence/observability callback."""
        self._on_change = callback

    def resolve_name(self, name: str) -> str:
        """Return the canonical selectable name or raise ``UnknownModeError``."""
        mode = self._registry.resolve(name)
        if self._registry.is_internal(mode.name):
            raise UnknownModeError(name, self._registry.selectable_names())
        return mode.name

    def _publish(self, mode: RuntimeMode) -> None:
        if self._app_state is not None:
            self._app_state.active_mode.set(mode)
        if self._on_change is not None:
            self._on_change(mode)


def build_mode_str(mode: RuntimeMode) -> str:
    c = mode.color
    return f"[{c}]{mode.badge} {mode.name}[/{c}][dim]  (shift+tab to cycle)[/dim]"
