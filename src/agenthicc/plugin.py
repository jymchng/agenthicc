"""Plugin system for agenthicc (PRD-13).

Plugins are installed Python packages that declare an ``agenthicc.plugins``
entry-point group. The :class:`PluginRegistry` discovers, loads, and tracks
them; each plugin's ``on_load`` method registers tools, hooks, slash commands,
agent types, and custom event handlers with the host runtime.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import Any, Callable

from agenthicc.tools.base import Tool

__all__ = [
    "AgenthiccPlugin",
    "PluginLoadError",
    "PluginManifest",
    "PluginRegistry",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PluginLoadError(Exception):
    """Raised when a plugin cannot be discovered or loaded."""


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@dataclass
class PluginManifest:
    """Metadata and runtime status for a single plugin."""

    name: str
    version: str
    entry_point: str
    status: str = "unloaded"  # "unloaded" | "loaded" | "error"
    error: str | None = None


# ---------------------------------------------------------------------------
# Plugin ABC
# ---------------------------------------------------------------------------


class AgenthiccPlugin(abc.ABC):
    """Base class every agenthicc plugin must inherit.

    Concrete plugins must implement :attr:`name` and :meth:`on_load`.
    All other lifecycle methods are optional no-ops.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable plugin identifier (lowercase, hyphens ok)."""

    @property
    def version(self) -> str:
        """Semantic version string."""
        return "0.0.0"

    @property
    def description(self) -> str:
        """Human-readable description of what the plugin does."""
        return ""

    @abc.abstractmethod
    def on_load(self, registry: "PluginRegistry", config: dict[str, Any] = {}) -> None:  # noqa: B006
        """Called once at startup. Register tools, hooks, and commands here."""

    def on_unload(self) -> None:
        """Called on hot-reload or shutdown. Release resources here."""

    def on_session_start(self, session_id: str) -> None:
        """Called at the beginning of each TUI session."""

    def on_session_end(self, session_id: str) -> None:
        """Called at the end of each TUI session."""


# ---------------------------------------------------------------------------
# Plugin registry
# ---------------------------------------------------------------------------


class PluginRegistry:
    """Discovers, loads, and tracks plugins; exposes typed registration methods.

    Plugins call the ``register_*`` methods from inside :meth:`AgenthiccPlugin.on_load`
    to contribute tools, hooks, slash commands, agent types, and event handlers.

    Parameters
    ----------
    event_processor:
        If supplied, :meth:`register_tool` emits a ``ToolRegistered`` kernel
        event so the tool appears in :class:`~agenthicc.kernel.AppState`.
    hook_runner:
        If supplied, :meth:`register_hook` adds hooks to the live
        :class:`~agenthicc.tools.hooks.HookRegistry`.
    input_bar_session:
        If supplied, :meth:`register_command` adds slash commands to the TUI
        completion menu.
    """

    def __init__(
        self,
        event_processor: Any | None = None,
        hook_runner: Any | None = None,
        input_bar_session: Any | None = None,
    ) -> None:
        self._processor = event_processor
        self._hook_runner = hook_runner
        self._input_bar_session = input_bar_session

        self._manifests: dict[str, PluginManifest] = {}
        self._plugins: dict[str, AgenthiccPlugin] = {}
        self._tools: list[Tool] = []
        self._agent_types: dict[str, type] = {}
        self._event_handlers: dict[str, Any] = {}

    # ── discovery ────────────────────────────────────────────────────────────

    def discover(self, plugin_names: list[str] | None = None) -> list[PluginManifest]:
        """Find all installed plugins via ``importlib.metadata`` entry points.

        Populates :attr:`_manifests` for each found plugin. Optionally filtered
        to *plugin_names*.

        Returns
        -------
        list[PluginManifest]
            Manifests for discovered plugins (status ``"unloaded"``).
        """
        from importlib.metadata import entry_points  # noqa: PLC0415

        eps = entry_points(group="agenthicc.plugins")
        manifests: list[PluginManifest] = []
        for ep in eps:
            if plugin_names and ep.name not in plugin_names:
                continue
            manifest = PluginManifest(
                name=ep.name,
                entry_point=ep.value,
                version="unknown",
            )
            self._manifests[ep.name] = manifest
            manifests.append(manifest)
        return manifests

    def load(self, name: str, config: dict[str, Any] | None = None) -> PluginManifest:
        """Load a single already-discovered plugin by *name*.

        Instantiates the plugin class, calls :meth:`AgenthiccPlugin.on_load`,
        and updates the manifest. If **any** exception is raised the manifest
        status is set to ``"error"`` and a warning is logged; the exception is
        **never re-raised**, so a broken plugin cannot crash startup.

        Parameters
        ----------
        name:
            The entry-point name used in ``discover()``.
        config:
            Optional dict forwarded to ``on_load()``.

        Returns
        -------
        PluginManifest
            Updated manifest (status ``"loaded"`` or ``"error"``).

        Raises
        ------
        PluginLoadError
            Only when *name* was never discovered (i.e. not in ``_manifests``).
        """
        manifest = self._manifests.get(name)
        if manifest is None:
            raise PluginLoadError(f"Plugin {name!r} not discovered yet; call discover() first")

        try:
            from importlib.metadata import entry_points  # noqa: PLC0415

            eps = {ep.name: ep for ep in entry_points(group="agenthicc.plugins")}
            ep = eps[name]
            plugin_cls = ep.load()
            plugin: AgenthiccPlugin = plugin_cls()
            plugin.on_load(self, config or {})
            manifest.version = plugin.version
            manifest.status = "loaded"
            self._plugins[name] = plugin
        except Exception as exc:  # noqa: BLE001
            manifest.status = "error"
            manifest.error = str(exc)
            logger.warning("Plugin %r failed to load: %s", name, exc)

        return manifest

    def reload(self, name: str, config: dict[str, Any] | None = None) -> PluginManifest:
        """Hot-reload a plugin by calling :meth:`on_unload` then :meth:`load`.

        Safe to call even if the plugin was never successfully loaded.
        """
        if name in self._plugins:
            try:
                self._plugins[name].on_unload()
            except Exception:  # noqa: BLE001
                pass
            del self._plugins[name]
        return self.load(name, config)

    def load_all(
        self,
        plugin_names: list[str],
        configs: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Discover and load all *plugin_names*, forwarding per-plugin configs.

        Parameters
        ----------
        plugin_names:
            Ordered list of entry-point names to load.
        configs:
            Optional mapping of plugin name → config dict.
        """
        self.discover(plugin_names)
        for name in plugin_names:
            self.load(name, (configs or {}).get(name))

    # ── registration API (called from AgenthiccPlugin.on_load) ───────────────

    def register_tool(self, tool: Tool) -> None:
        """Register a tool and optionally emit a ``ToolRegistered`` kernel event.

        The tool is appended to the internal list regardless of whether a
        processor is available. When a processor *is* available the event is
        scheduled on the running event loop so it flows through the reducer.
        """
        self._tools.append(tool)
        if self._processor is not None:
            import asyncio  # noqa: PLC0415

            from agenthicc.kernel import Event  # noqa: PLC0415

            event = Event.create(
                "ToolRegistered",
                {
                    "tool_id": tool.name,
                    "name": tool.name,
                    "description": tool.description,
                    "parameters_schema": tool.parameters,
                    "is_builtin": False,
                    "source_agent_id": None,
                },
            )
            try:
                loop = asyncio.get_event_loop()
                loop.call_soon_threadsafe(
                    lambda e=event: asyncio.ensure_future(self._processor.emit(e))
                )
            except RuntimeError:
                # No running loop — best-effort; tests that need the event use
                # the integration fixture which has a running processor task.
                pass

    def register_hook(self, entity_type: str, stage: str, hook: Any) -> None:
        """Register a lifecycle hook with the live hook runner.

        No-op when the registry was constructed without a *hook_runner*.
        """
        if self._hook_runner is not None:
            self._hook_runner.registry.register(entity_type, stage, hook)

    def register_command(self, spec: Any) -> None:
        """Register a slash command in the TUI ``InputBarSession``.

        No-op when the registry was constructed without an *input_bar_session*.
        """
        if self._input_bar_session is not None:
            self._input_bar_session.register_command(spec)

    def register_agent_type(self, type_name: str, agent_cls: type) -> None:
        """Register an agent class for use via the ``agent_spawn`` comm tool."""
        self._agent_types[type_name] = agent_cls

    def register_event_handler(
        self, event_type: str, handler_fn: Callable[..., Any]
    ) -> None:
        """Extend the ``root_reducer`` dispatch table with a custom event handler."""
        self._event_handlers[event_type] = handler_fn

    # ── properties ───────────────────────────────────────────────────────────

    @property
    def tools(self) -> list[Tool]:
        """Snapshot of all registered tools."""
        return list(self._tools)

    @property
    def agent_types(self) -> dict[str, type]:
        """Snapshot of all registered agent-type classes keyed by type name."""
        return dict(self._agent_types)

    @property
    def event_handlers(self) -> dict[str, Any]:
        """Snapshot of all registered custom event handlers."""
        return dict(self._event_handlers)

    @property
    def manifests(self) -> list[PluginManifest]:
        """All known manifests (discovered plugins) in insertion order."""
        return list(self._manifests.values())
