---
title: "Plugin System"
status: draft
version: 0.1.0
created: 2025-01-01
authors:
  - platform-ai-team
reviewers:
  - backend-lead
  - dx-lead
related_prds:
  - PRD-01  # Application State and Event Bus
  - PRD-03  # Agent Runtime and Communication Tools
  - PRD-04  # Tool Execution and Hooks
  - PRD-06  # TUI and Observability
  - PRD-12  # MCP Integration
supersedes: []
tags:
  - plugins
  - extensibility
  - tools
  - hooks
  - slash-commands
---

# PRD-13: Plugin System

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Goals and Non-Goals](#2-goals-and-non-goals)
3. [Architecture and Design](#3-architecture-and-design)
4. [Data Structures and Interfaces](#4-data-structures-and-interfaces)
5. [What Plugins Can Contribute](#5-what-plugins-can-contribute)
6. [Hot Reload](#6-hot-reload)
7. [Skills Integration](#7-skills-integration)
8. [Configuration Reference](#8-configuration-reference)
9. [Creating a Plugin — Full Working Example](#9-creating-a-plugin--full-working-example)
10. [Implementation Plan](#10-implementation-plan)
11. [Tests](#11-tests)
12. [Open Questions](#12-open-questions)

---

## 1. Executive Summary

PRD-13 specifies the **Plugin System** for agenthicc. Users need to extend agenthicc with custom tools, lifecycle hooks, agent types, slash commands, and event reducers without modifying any core source file. Plugins are installable Python packages that declare a standard `agenthicc.plugins` entry point group; the runtime discovers and loads them automatically at startup.

The design integrates cleanly with the existing agenthicc contracts:

- Tools contributed by plugins become `ToolRegistration` records in `AppState.tools` and flow through the standard `AgenthiccToolExecutor` pipeline (permission check, hooks, timeout, events).
- Hooks contributed by plugins are registered into the existing `HookRegistry` / `HookRunner` system from PRD-04.
- Agent types contributed by plugins are stored in `AppState.agent_types` and are available via the `agent_spawn` comm tool.
- Slash commands are registered in the TUI input bar and can also supply `SKILL.md` files for AI agent context.
- Custom event handlers extend the `root_reducer` dispatch table.

A `PluginRegistry` singleton orchestrates discovery via `importlib.metadata.entry_points`, calls lifecycle methods on each plugin, and exposes typed `register_*` methods for each contribution category. Hot reload (`agenthicc plugins reload <name>`) is supported without restarting the application.

---

## 2. Goals and Non-Goals

### 2.1 Goals

| # | Goal |
|---|------|
| G-01 | Discover installed plugins at startup via `importlib.metadata.entry_points(group="agenthicc.plugins")` |
| G-02 | Let plugins register Tools, Hooks, Slash Commands, Agent Types, and custom Event Handlers through a typed `PluginRegistry` API |
| G-03 | Registered tools are available to agents through the existing `AgenthiccToolExecutor` pipeline |
| G-04 | Registered hooks wire into `HookRunner` exactly as manually registered hooks do |
| G-05 | Plugin config from `[plugins.<name>]` TOML sections is forwarded to `on_load(registry, config={})` |
| G-06 | Hot reload via `agenthicc plugins reload <name>` unloads and reloads a plugin without restarting |
| G-07 | Plugins that register slash commands may also supply `skills/plugin-name/SKILL.md` files |
| G-08 | Provide a runnable `AgenthiccPlugin` ABC and clear packaging instructions for third-party developers |
| G-09 | Emit `PluginLoaded` / `PluginUnloaded` events on the kernel event bus for observability |
| G-10 | Provide runnable pytest unit, integration, and E2E tests |

### 2.2 Non-Goals

| # | Non-Goal |
|---|----------|
| NG-01 | Plugin sandboxing or security isolation (plugins are trusted Python packages) |
| NG-02 | Plugin marketplace or remote installation (pip install is the distribution mechanism) |
| NG-03 | Cross-language plugins (Python only in v1) |
| NG-04 | Plugin versioning / dependency resolution beyond what pip provides |
| NG-05 | UI panels contributed by plugins (deferred to PRD-06 evolution) |

---

## 3. Architecture and Design

### 3.1 High-Level Component Diagram

```
+------------------------------------------------------------------+
|                        AGENTHICC CORE                            |
|                                                                  |
|  AppState.tools      AppState.hooks    AppState.agent_types      |
|  AppState.agents     EventProcessor    HookRunner                |
|                                                                  |
|  +--------------+    +---------------+    +-----------------+   |
|  | root_reducer |    | HookRegistry  |    | TUI Input Bar   |   |
|  | (dispatch)   |    | (entity/stage)|    | (slash cmds)    |   |
|  +--------------+    +---------------+    +-----------------+   |
+------------------------------------------------------------------+
          ^                   ^                     ^
          |                   |                     |
          | register_event_handler  register_hook  register_command
          |                   |                     |
+------------------------------------------------------------------+
|                       PLUGIN REGISTRY                            |
|                                                                  |
|  discover()     -- entry_points(group="agenthicc.plugins")       |
|  load(name)     -- instantiates plugin, calls on_load(registry)  |
|  unload(name)   -- calls on_unload(), reverses registrations     |
|  reload(name)   -- unload + load                                 |
|                                                                  |
|  register_tool(tool)                                             |
|  register_hook(entity_type, stage, hook_instance)               |
|  register_command(CommandSpec)                                   |
|  register_agent_type(name, agent_cls)                            |
|  register_event_handler(event_type, handler_fn)                  |
+------------------------------------------------------------------+
          ^
          | on_load(registry, config={})
          |
+------------------------------------------------------------------+
|                       INSTALLED PLUGINS                           |
|                                                                  |
|  agenthicc-git        agenthicc-jira       my_custom_plugin      |
|  [Tool: git_commit]   [Tool: jira_create]  [Tool: my_greeting]   |
|  [Hook: pre-commit]   [Hook: ticket_hook]  [Cmd: /greet]         |
|  [Cmd: /git-diff]     [Cmd: /jira-issue]                         |
+------------------------------------------------------------------+
```

### 3.2 Discovery and Load Sequence

```
AgenthiccApp.__init__
  |
  +-> PluginRegistry(event_processor, hook_runner, ...)
  |
  +-> PluginRegistry.discover()
        |
        +-> importlib.metadata.entry_points(group="agenthicc.plugins")
        |     -> [EntryPoint(name="agenthicc-git", ...), ...]
        |
        for each entry_point:
          |
          +-> plugin_cls = entry_point.load()
          +-> plugin = plugin_cls()
          +-> config = app_config.plugins.get(plugin.name, {})
          +-> plugin.on_load(registry, config=config)
          |     -> registry.register_tool(GitCommitTool())
          |     -> registry.register_command(CommandSpec("/git-diff", "..."))
          |
          +-> EventProcessor.emit(PluginLoaded{name, version})
          +-> PluginRegistry._loaded[plugin.name] = PluginManifest(...)
```

### 3.3 Tool Registration Flow

```
plugin.on_load(registry):
    registry.register_tool(MyTool())
          |
          v
    EventProcessor.emit(ToolRegistered{
        name="my_tool",
        description=MyTool.description,
        parameters_schema=MyTool.parameters,
        is_builtin=False,
    })
          |
          v
    root_reducer handles ToolRegistered
          |
          v
    AppState.tools["my_tool"] = ToolRegistration(...)
          |
          v
    Agent calls "my_tool" -> AgenthiccToolExecutor.execute(MyTool(), args, ctx)
```

---

## 4. Data Structures and Interfaces

### 4.1 AgenthiccPlugin ABC

```python
# src/agenthicc/plugins/base.py

from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agenthicc.plugins.registry import PluginRegistry

__all__ = ["AgenthiccPlugin"]


class AgenthiccPlugin(abc.ABC):
    """Abstract base class every agenthicc plugin must inherit.

    Subclasses must declare :attr:`name` and :attr:`version` as class
    attributes.  All lifecycle methods have default no-op implementations
    so concrete plugins only override the stages they need.

    Packaging::

        # pyproject.toml of the plugin package
        [project.entry-points."agenthicc.plugins"]
        my_plugin = "my_package.plugin:MyPlugin"
    """

    #: Stable slug; used as the key in ``AppState`` and config sections.
    name: str = ""

    #: SemVer string, e.g. ``"1.2.3"``.
    version: str = "0.0.0"

    #: Human-readable description shown in ``agenthicc plugins list``.
    description: str = ""

    def on_load(
        self,
        registry: "PluginRegistry",
        config: dict[str, Any] | None = None,
    ) -> None:
        """Called once when the plugin is first loaded.

        Register tools, hooks, commands, agent types, and event
        handlers here.  ``config`` contains values from the
        ``[plugins.<name>]`` TOML section (empty dict if absent).

        :param registry: The live :class:`PluginRegistry`; call
            ``registry.register_*`` methods to contribute capabilities.
        :param config: Plugin-specific configuration from TOML.
        """

    def on_unload(self) -> None:
        """Called when the plugin is unloaded (e.g. during hot reload).

        Release external resources (open files, network connections,
        background tasks) here.  Registered tools, hooks, and commands
        are automatically deregistered by the registry before this
        method is called.
        """

    def on_session_start(self, session: dict[str, Any]) -> None:
        """Called at the start of every new agenthicc session."""

    def on_session_end(self, session: dict[str, Any]) -> None:
        """Called at the end of every agenthicc session."""
```

### 4.2 PluginRegistry

```python
# src/agenthicc/plugins/registry.py

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agenthicc.kernel import Event, EventProcessor
from agenthicc.tools.base import Tool
from agenthicc.tools.hooks import HookRegistry, LifecycleHook
from agenthicc.plugins.base import AgenthiccPlugin

__all__ = ["CommandSpec", "PluginLoadError", "PluginManifest", "PluginRegistry"]

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "agenthicc.plugins"


@dataclass
class CommandSpec:
    """Specification for a TUI slash command contributed by a plugin.

    :param command: The command string including the leading slash
        (e.g. ``"/greet"``).
    :param description: Short description shown in the TUI help overlay.
    :param handler: Optional async callable invoked when the command is
        entered.  When None, the command is registered as a label only
        and the TUI routes it via the normal input pipeline.
    """

    command: str
    description: str
    handler: Callable[..., Any] | None = None

    def __post_init__(self) -> None:
        if not self.command.startswith("/"):
            raise ValueError(
                f"CommandSpec.command must start with '/'; got {self.command!r}"
            )


@dataclass
class PluginManifest:
    """Runtime record of a loaded plugin.

    Stored in :attr:`PluginRegistry._loaded` keyed by ``plugin.name``.
    """

    name: str
    version: str
    entry_point: str
    status: str = "loaded"  # "loaded" | "error" | "unloaded"
    error: str | None = None

    #: Snapshot of registered tool names for this plugin (used during unload).
    registered_tool_names: list[str] = field(default_factory=list)
    #: Snapshot of registered hook keys for this plugin.
    registered_hook_keys: list[tuple[str, str]] = field(default_factory=list)
    #: Snapshot of registered command strings for this plugin.
    registered_commands: list[str] = field(default_factory=list)
    #: Snapshot of registered agent type names for this plugin.
    registered_agent_types: list[str] = field(default_factory=list)
    #: Snapshot of registered event handler types for this plugin.
    registered_event_handlers: list[str] = field(default_factory=list)


class PluginLoadError(Exception):
    """Raised when a plugin fails to load."""


class PluginRegistry:
    """Discovers, loads, and manages the lifecycle of agenthicc plugins.

    This is the object passed as ``registry`` to
    :meth:`AgenthiccPlugin.on_load`.  It provides typed ``register_*``
    methods for each contribution category, and wires contributions into
    the running application.

    :param event_processor: Kernel event processor.
    :param hook_registry: Hook registry from PRD-04.
    :param tui_command_registry: The TUI input bar's command table.
        When None (non-TUI mode), slash command registrations are stored
        but not rendered.
    :param reducer_dispatch: Mutable dict that ``root_reducer`` consults
        to route events to handlers.  Plugins extend this dict to add
        custom event handling.
    """

    def __init__(
        self,
        event_processor: EventProcessor,
        hook_registry: HookRegistry,
        tui_command_registry: dict[str, CommandSpec] | None = None,
        reducer_dispatch: dict[str, Any] | None = None,
    ) -> None:
        self._events = event_processor
        self._hooks = hook_registry
        self._tui_commands: dict[str, CommandSpec] = (
            tui_command_registry if tui_command_registry is not None else {}
        )
        self._reducer_dispatch: dict[str, Any] = (
            reducer_dispatch if reducer_dispatch is not None else {}
        )
        self._tools: dict[str, Tool] = {}
        self._agent_types: dict[str, type] = {}
        self._loaded: dict[str, PluginManifest] = {}
        self._plugin_instances: dict[str, AgenthiccPlugin] = {}

    # ── Discovery and lifecycle ───────────────────────────────────────

    def discover(self, app_config: dict[str, Any] | None = None) -> None:
        """Discover and load all installed plugins.

        :param app_config: The full parsed TOML config dict.
        """
        app_config = app_config or {}
        plugins_config: dict[str, dict] = app_config.get("plugins", {})
        eps = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
        for ep in eps:
            try:
                self.load(ep.name, ep.value, plugins_config.get(ep.name, {}))
            except PluginLoadError as exc:
                logger.error(
                    "PluginRegistry: skipping plugin %r: %s", ep.name, exc
                )

    def load(
        self,
        plugin_name: str,
        entry_point_value: str,
        config: dict[str, Any] | None = None,
    ) -> PluginManifest:
        """Load a single plugin by entry-point value.

        :param plugin_name: Slug from the entry point name.
        :param entry_point_value: ``"module.path:ClassName"`` string.
        :param config: Plugin-specific config dict.
        :returns: :class:`PluginManifest` for the loaded plugin.
        :raises PluginLoadError: On import, instantiation, or on_load failure.
        """
        if plugin_name in self._loaded:
            raise PluginLoadError(
                f"Plugin {plugin_name!r} is already loaded; "
                "call reload() to update it"
            )
        try:
            module_path, _, class_name = entry_point_value.partition(":")
            module = importlib.import_module(module_path)
            plugin_cls: type[AgenthiccPlugin] = getattr(module, class_name)
            plugin = plugin_cls()
        except Exception as exc:
            raise PluginLoadError(
                f"Failed to import plugin {plugin_name!r} "
                f"from {entry_point_value!r}: {exc}"
            ) from exc

        manifest = PluginManifest(
            name=plugin.name or plugin_name,
            version=plugin.version,
            entry_point=entry_point_value,
        )
        self._loaded[manifest.name] = manifest
        self._plugin_instances[manifest.name] = plugin

        try:
            plugin.on_load(self, config=config or {})
        except Exception as exc:
            del self._loaded[manifest.name]
            del self._plugin_instances[manifest.name]
            raise PluginLoadError(
                f"Plugin {plugin_name!r} on_load() raised: {exc}"
            ) from exc

        manifest.status = "loaded"
        self._emit_sync(
            "PluginLoaded",
            {"name": manifest.name, "version": manifest.version},
        )
        logger.info(
            "PluginRegistry: loaded %r v%s", manifest.name, manifest.version
        )
        return manifest

    def unload(self, plugin_name: str) -> None:
        """Unload a plugin and reverse all its registrations.

        :param plugin_name: The ``plugin.name`` slug.
        :raises KeyError: If the plugin is not loaded.
        """
        manifest = self._loaded.get(plugin_name)
        if manifest is None:
            raise KeyError(f"Plugin {plugin_name!r} is not loaded")

        for tool_name in manifest.registered_tool_names:
            self._tools.pop(tool_name, None)
        for cmd in manifest.registered_commands:
            self._tui_commands.pop(cmd, None)
        for agent_type_name in manifest.registered_agent_types:
            self._agent_types.pop(agent_type_name, None)
        for event_type in manifest.registered_event_handlers:
            self._reducer_dispatch.pop(event_type, None)

        plugin = self._plugin_instances.pop(plugin_name, None)
        if plugin is not None:
            try:
                plugin.on_unload()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "PluginRegistry: on_unload() raised for %r: %s",
                    plugin_name, exc,
                )

        manifest.status = "unloaded"
        del self._loaded[plugin_name]
        self._emit_sync("PluginUnloaded", {"name": plugin_name})
        logger.info("PluginRegistry: unloaded %r", plugin_name)

    def reload(
        self, plugin_name: str, config: dict[str, Any] | None = None
    ) -> PluginManifest:
        """Unload then reload a plugin without restarting."""
        manifest = self._loaded.get(plugin_name)
        if manifest is None:
            raise KeyError(f"Plugin {plugin_name!r} is not loaded")
        entry_point = manifest.entry_point
        self.unload(plugin_name)
        return self.load(plugin_name, entry_point, config)

    # ── Registration API ─────────────────────────────────────────────

    def register_tool(self, tool: Tool) -> None:
        """Register a tool contributed by a plugin."""
        if tool.name in self._tools:
            raise ValueError(
                f"PluginRegistry: tool {tool.name!r} is already registered"
            )
        self._tools[tool.name] = tool
        self._emit_sync(
            "ToolRegistered",
            {
                "tool_id": _new_id(),
                "name": tool.name,
                "description": tool.description,
                "parameters_schema": tool.parameters,
                "is_builtin": False,
                "source_code": None,
            },
        )
        self._track("tool", tool.name)
        logger.debug("PluginRegistry: registered tool %r", tool.name)

    def register_hook(
        self,
        entity_type: str,
        stage: str,
        hook_instance: LifecycleHook,
    ) -> None:
        """Wire a lifecycle hook into the running HookRegistry."""
        self._hooks.register(entity_type, stage, hook_instance)
        self._track("hook", (entity_type, stage))
        logger.debug(
            "PluginRegistry: registered hook %r on %s/%s",
            type(hook_instance).__name__, entity_type, stage,
        )

    def register_command(self, spec: CommandSpec) -> None:
        """Register a TUI slash command."""
        if spec.command in self._tui_commands:
            raise ValueError(
                f"PluginRegistry: command {spec.command!r} is already registered"
            )
        self._tui_commands[spec.command] = spec
        self._track("command", spec.command)
        logger.debug("PluginRegistry: registered command %r", spec.command)

    def register_agent_type(self, name: str, agent_cls: type) -> None:
        """Register a custom agent class available via agent_spawn."""
        if name in self._agent_types:
            raise ValueError(
                f"PluginRegistry: agent type {name!r} is already registered"
            )
        self._agent_types[name] = agent_cls
        self._emit_sync(
            "AgentTypeRegistered",
            {"agent_type": name, "class_qualname": agent_cls.__qualname__},
        )
        self._track("agent_type", name)
        logger.debug("PluginRegistry: registered agent type %r", name)

    def register_event_handler(
        self,
        event_type: str,
        handler_fn: Callable[[Any, Any], Any],
    ) -> None:
        """Extend the root_reducer dispatch table with a custom handler."""
        if event_type in self._reducer_dispatch:
            raise ValueError(
                f"PluginRegistry: event handler for {event_type!r} already registered"
            )
        self._reducer_dispatch[event_type] = handler_fn
        self._track("event_handler", event_type)
        logger.debug(
            "PluginRegistry: registered event handler for %r", event_type
        )

    def get_tool(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def get_command(self, command: str) -> CommandSpec | None:
        return self._tui_commands.get(command)

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def loaded_plugins(self) -> list[PluginManifest]:
        return list(self._loaded.values())

    # ── Internal helpers ─────────────────────────────────────────────

    def _emit_sync(self, event_type: str, payload: dict[str, Any]) -> None:
        import asyncio  # noqa: PLC0415
        event = Event.create(event_type, payload)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._events.emit(event))
            else:
                loop.run_until_complete(self._events.emit(event))
        except RuntimeError:
            pass

    def _track(self, category: str, value: Any) -> None:
        """Record a registration in the currently-loading plugin's manifest."""
        for manifest in self._loaded.values():
            if manifest.status != "loaded":
                if category == "tool":
                    manifest.registered_tool_names.append(value)
                elif category == "hook":
                    manifest.registered_hook_keys.append(value)
                elif category == "command":
                    manifest.registered_commands.append(value)
                elif category == "agent_type":
                    manifest.registered_agent_types.append(value)
                elif category == "event_handler":
                    manifest.registered_event_handlers.append(value)
                return


def _new_id() -> str:
    from uuid import uuid4  # noqa: PLC0415
    return uuid4().hex
```

### 4.3 PluginLoadError and PluginManifest

See the full definitions above in `PluginRegistry`.  Both are exported from `src/agenthicc/plugins/__init__.py`.

---

## 5. What Plugins Can Contribute

### 5.1 Tools

```python
registry.register_tool(MyTool())
```

The tool becomes a `ToolRegistration` in `AppState.tools` via a `ToolRegistered` event. Agents invoke it by name through `AgenthiccToolExecutor`. The full pipeline applies: permission check, before-hooks, timeout, after-hooks, error-hooks, `ToolCallStarted` / `ToolCallComplete` events.

### 5.2 Hooks

```python
registry.register_hook("tool", "before", MyPreToolHook())
registry.register_hook("tool", "after", MyAuditHook())
registry.register_hook("task", "error", MyTaskErrorHook())
```

Hooks registered by plugins are stored in the shared `HookRegistry` and are fired by `HookRunner` exactly as manually registered hooks are.  The `entity_type` values follow the PRD-04 convention (`"tool"`, `"intent"`, `"task"`, `"agent"`).  Stage values are `"before"`, `"after"`, `"error"`.

### 5.3 Slash Commands

```python
registry.register_command(
    CommandSpec("/greet", "Greet someone by name", handler=greet_handler)
)
```

The `CommandSpec` is stored in the TUI command registry.  When the user types `/greet` in the input bar, the TUI either calls `spec.handler(...)` directly (if provided) or routes the command through the normal input pipeline.

### 5.4 Agent Types

```python
registry.register_agent_type("MySpecialistAgent", MySpecialistAgentClass)
```

The type is stored in `AppState.agent_types` via an `AgentTypeRegistered` event.  Agents can then spawn instances using the `agent_spawn` comm tool:

```python
await comm.agent_spawn(agent_type="MySpecialistAgent", config={...})
```

### 5.5 Custom Event Handlers

```python
def handle_my_event(state: AppState, event: Event) -> AppState:
    return replace(state, ...)

registry.register_event_handler("MyCustomEvent", handle_my_event)
```

The handler is inserted into the `root_reducer`'s dispatch table.

---

## 6. Hot Reload

`agenthicc plugins reload <name>` calls `PluginRegistry.reload()`:

```
PluginRegistry.reload("agenthicc-git")
  |
  +-> manifest = self._loaded["agenthicc-git"]
  +-> self.unload("agenthicc-git")
  |     -> deregisters tools, hooks, commands
  |     -> calls plugin.on_unload()
  |     -> emits PluginUnloaded event
  |
  +-> importlib.reload(module)   # refreshes compiled bytecode
  +-> self.load("agenthicc-git", manifest.entry_point, current_config)
        -> re-instantiates plugin class
        -> calls plugin.on_load(registry, config=...)
        -> emits PluginLoaded event
```

**Limitation**: hooks contributed by the old instance remain in `HookRegistry` as no-ops in v1 because `HookRegistry` does not support removal (see OQ-01).  The new instance's hooks are added alongside them.

---

## 7. Skills Integration

Plugins that register slash commands may supply `SKILL.md` files under a `skills/` directory relative to their package root.  The `PluginRegistry` copies these into the session skills directory on load.

### 7.1 Package Layout

```
my_plugin_package/
  plugin.py
  skills/
    my-plugin-name/
      SKILL.md
```

### 7.2 Copy Mechanism

```python
import importlib.resources
from pathlib import Path

def _copy_skills(plugin: AgenthiccPlugin, session_skills_dir: Path) -> None:
    try:
        pkg_name = type(plugin).__module__.split(".")[0]
        pkg_path = importlib.resources.files(pkg_name)
        skills_src = pkg_path / "skills"
        if not skills_src.is_dir():
            return
        dest = session_skills_dir / plugin.name
        dest.mkdir(parents=True, exist_ok=True)
        for skill_file in skills_src.rglob("*.md"):
            relative = skill_file.relative_to(skills_src)
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(skill_file.read_bytes())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "PluginRegistry: could not copy skills for %r: %s", plugin.name, exc
        )
```

---

## 8. Configuration Reference

```toml
# agenthicc.toml

# ── Plugin enable list ────────────────────────────────────────────────
# If omitted, ALL discovered plugins are loaded.
[tools]
plugins = ["agenthicc-git", "agenthicc-jira"]

# ── Per-plugin configuration ──────────────────────────────────────────
[plugins.agenthicc-git]
default_branch = "main"
sign_commits = true

[plugins.agenthicc-jira]
base_url = "https://myorg.atlassian.net"
project_key = "PLAT"
api_token = "${JIRA_API_TOKEN}"

[plugins.my-custom-plugin]
greeting_style = "formal"
```

---

## 9. Creating a Plugin — Full Working Example

### 9.1 Package Layout

```
my_greeting_plugin/
  pyproject.toml
  src/
    my_greeting_plugin/
      __init__.py
      plugin.py
      tools.py
      hooks.py
      skills/
        my-greeting/
          SKILL.md
```

### 9.2 pyproject.toml

```toml
[project]
name = "my-greeting-plugin"
version = "1.0.0"
dependencies = ["agenthicc>=0.1.0"]

[project.entry-points."agenthicc.plugins"]
my-greeting = "my_greeting_plugin.plugin:MyGreetingPlugin"
```

### 9.3 Tool

```python
# src/my_greeting_plugin/tools.py

from __future__ import annotations
from typing import Any
from agenthicc.tools.base import Tool


class GreetingTool(Tool):
    name = "my_greeting"
    description = "Say hello to someone by name."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Person to greet."},
            "style": {
                "type": "string",
                "enum": ["casual", "formal"],
                "default": "casual",
            },
        },
        "required": ["name"],
    }

    def __init__(self, style_override: str | None = None) -> None:
        self._style_override = style_override

    async def execute(self, args: dict[str, Any], context: dict) -> str:
        person = args["name"]
        style = self._style_override or args.get("style", "casual")
        if style == "formal":
            return f"Good day, {person}. How may I assist you?"
        return f"Hey {person}! What's up?"
```

### 9.4 Hook

```python
# src/my_greeting_plugin/hooks.py

import logging
from typing import Any
from agenthicc.tools.hooks import LifecycleHook

logger = logging.getLogger(__name__)


class GreetingAuditHook(LifecycleHook):
    async def on_after(self, entity: Any, result: Any, ctx: Any) -> None:
        if getattr(entity, "name", "") == "my_greeting":
            logger.info("GreetingAudit: greeting produced -> %r", result)
```

### 9.5 Plugin Class

```python
# src/my_greeting_plugin/plugin.py

from __future__ import annotations
from typing import TYPE_CHECKING, Any
from agenthicc.plugins.base import AgenthiccPlugin
from agenthicc.plugins.registry import CommandSpec

if TYPE_CHECKING:
    from agenthicc.plugins.registry import PluginRegistry

from my_greeting_plugin.tools import GreetingTool
from my_greeting_plugin.hooks import GreetingAuditHook


class MyGreetingPlugin(AgenthiccPlugin):
    name = "my-greeting"
    version = "1.0.0"
    description = "Provides a greeting tool and /greet slash command."

    def on_load(
        self,
        registry: "PluginRegistry",
        config: dict[str, Any] | None = None,
    ) -> None:
        config = config or {}
        style = config.get("greeting_style", "casual")
        registry.register_tool(GreetingTool(style_override=style))
        registry.register_hook("tool", "after", GreetingAuditHook())
        registry.register_command(
            CommandSpec("/greet", "Greet someone: /greet <name>")
        )

    def on_unload(self) -> None:
        pass  # no external resources to release
```

### 9.6 SKILL.md

```markdown
<!-- src/my_greeting_plugin/skills/my-greeting/SKILL.md -->

# Skill: my_greeting

## Purpose

Use the `my_greeting` tool when you want to produce a polite greeting
for a person by name.  Supports `casual` (default) and `formal` styles.

## Example tool call

```json
{
  "name": "my_greeting",
  "input": {"name": "Alice", "style": "formal"}
}
```

## Expected result

```
"Good day, Alice. How may I assist you?"
```
```

---

## 10. Implementation Plan

### 10.1 Phase 1 — Core ABC and Registry (Week 1)

| Task | File | Notes |
|---|---|---|
| Define `AgenthiccPlugin` ABC | `src/agenthicc/plugins/base.py` | New file |
| Define `PluginRegistry`, `CommandSpec`, `PluginManifest`, `PluginLoadError` | `src/agenthicc/plugins/registry.py` | New file |
| Add `plugins/__init__.py` | `src/agenthicc/plugins/__init__.py` | Export public API |
| Unit tests | `tests/unit/test_plugin_registry.py` | Mock event processor |

### 10.2 Phase 2 — Discovery and Lifecycle (Week 1-2)

| Task | File | Notes |
|---|---|---|
| `PluginRegistry.discover()` via `entry_points` | `src/agenthicc/plugins/registry.py` | Guard import errors with `PluginLoadError` |
| `load()` / `unload()` / `reload()` | `src/agenthicc/plugins/registry.py` | |
| Wire into `AgenthiccApp.__init__` | `src/agenthicc/api/server.py` | After kernel init |
| `PluginLoaded` / `PluginUnloaded` reducer branch | `src/agenthicc/kernel/reducer.py` | |
| Integration tests | `tests/integration/test_plugin_integration.py` | |

### 10.3 Phase 3 — Config, Skills, and CLI (Week 2)

| Task | File | Notes |
|---|---|---|
| Parse `[tools].plugins` and `[plugins.*]` TOML | `src/agenthicc/config.py` | |
| Skills copy on load | `src/agenthicc/plugins/registry.py` | `_copy_skills()` helper |
| `agenthicc plugins list` CLI subcommand | `src/agenthicc/cli.py` | |
| `agenthicc plugins reload <name>` CLI subcommand | `src/agenthicc/cli.py` | |
| E2E tests | `tests/e2e/test_plugin_e2e.py` | MockTransport |

---

## 11. Tests

### 11.1 Unit Tests

```python
# tests/unit/test_plugin_registry.py

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import MagicMock
from typing import Any

from agenthicc.kernel import AppState, EventProcessor
from agenthicc.plugins.base import AgenthiccPlugin
from agenthicc.plugins.registry import (
    CommandSpec,
    PluginLoadError,
    PluginManifest,
    PluginRegistry,
)
from agenthicc.tools.base import Tool
from agenthicc.tools.hooks import HookRegistry, LifecycleHook, Rejection


class PingTool(Tool):
    name = "ping"
    description = "Returns pong."
    parameters: dict = {}
    async def execute(self, args, context): return "pong"


class TrackingHook(LifecycleHook):
    def __init__(self):
        self.before_calls: list = []
    async def on_before(self, entity, ctx) -> Rejection | None:
        self.before_calls.append(entity)
        return None


@pytest.fixture
def event_processor():
    return EventProcessor(initial_state=AppState.create())


@pytest.fixture
def hook_registry():
    return HookRegistry()


@pytest.fixture
def plugin_registry(event_processor, hook_registry):
    return PluginRegistry(
        event_processor=event_processor,
        hook_registry=hook_registry,
    )


class TestRegisterTool:
    def test_tool_available_after_registration(self, plugin_registry):
        plugin_registry.register_tool(PingTool())
        assert plugin_registry.get_tool("ping") is not None

    def test_duplicate_tool_raises(self, plugin_registry):
        plugin_registry.register_tool(PingTool())
        with pytest.raises(ValueError, match="already registered"):
            plugin_registry.register_tool(PingTool())

    def test_all_tools_includes_registered(self, plugin_registry):
        plugin_registry.register_tool(PingTool())
        assert any(t.name == "ping" for t in plugin_registry.all_tools())

    @pytest.mark.asyncio
    async def test_tool_registered_event_emitted(
        self, plugin_registry, event_processor
    ):
        plugin_registry.register_tool(PingTool())
        await asyncio.sleep(0)
        await event_processor.drain()
        state = event_processor.get_state()
        assert "ping" in state.tools


class TestRegisterHook:
    @pytest.mark.asyncio
    async def test_hook_fires_on_tool_call(
        self, plugin_registry, hook_registry, event_processor
    ):
        from agenthicc.tools.executor import AgenthiccToolExecutor
        from agenthicc.tools.hooks import HookRunner

        hook = TrackingHook()
        plugin_registry.register_hook("tool", "before", hook)
        runner = HookRunner(registry=hook_registry)
        executor = AgenthiccToolExecutor(
            event_processor=event_processor, hook_runner=runner
        )
        envelope = await executor.execute(PingTool(), {}, {})
        assert envelope.ok is True
        assert len(hook.before_calls) == 1

    def test_hook_stored_in_hook_registry(self, plugin_registry, hook_registry):
        hook = TrackingHook()
        plugin_registry.register_hook("tool", "before", hook)
        assert hook in hook_registry.hooks_for("tool", "before")


class TestRegisterCommand:
    def test_command_available_after_registration(self, plugin_registry):
        spec = CommandSpec("/ping", "Ping the system")
        plugin_registry.register_command(spec)
        assert plugin_registry.get_command("/ping") is spec

    def test_duplicate_command_raises(self, plugin_registry):
        plugin_registry.register_command(CommandSpec("/ping", "first"))
        with pytest.raises(ValueError, match="already registered"):
            plugin_registry.register_command(CommandSpec("/ping", "second"))

    def test_command_without_leading_slash_raises(self):
        with pytest.raises(ValueError, match="'/'"):
            CommandSpec("ping", "no slash")


class TestRegisterAgentType:
    def test_agent_type_stored(self, plugin_registry):
        class FakeAgent: pass
        plugin_registry.register_agent_type("FakeAgent", FakeAgent)
        assert plugin_registry._agent_types.get("FakeAgent") is FakeAgent

    def test_duplicate_raises(self, plugin_registry):
        class A: pass
        plugin_registry.register_agent_type("A", A)
        with pytest.raises(ValueError, match="already registered"):
            plugin_registry.register_agent_type("A", A)


class TestRegisterEventHandler:
    def test_handler_stored_in_dispatch(self, plugin_registry):
        dispatch: dict = {}
        plugin_registry._reducer_dispatch = dispatch
        def my_handler(state, event): return state
        plugin_registry.register_event_handler("MyEvent", my_handler)
        assert dispatch.get("MyEvent") is my_handler

    def test_duplicate_raises(self, plugin_registry):
        def h(s, e): return s
        plugin_registry.register_event_handler("E", h)
        with pytest.raises(ValueError, match="already registered"):
            plugin_registry.register_event_handler("E", h)


class TestPluginLifecycle:
    def test_unload_deregisters_tool(self, plugin_registry):
        class SimplePlugin(AgenthiccPlugin):
            name = "sp"
            version = "0.0.1"
            def on_load(self, registry, config=None):
                registry.register_tool(PingTool())

        plugin = SimplePlugin()
        manifest = PluginManifest(
            name=plugin.name, version=plugin.version, entry_point="x:y"
        )
        plugin_registry._loaded[plugin.name] = manifest
        plugin_registry._plugin_instances[plugin.name] = plugin
        plugin.on_load(plugin_registry)
        manifest.status = "loaded"

        plugin_registry.unload(plugin.name)
        assert plugin_registry.get_tool("ping") is None

    def test_unload_unknown_plugin_raises(self, plugin_registry):
        with pytest.raises(KeyError):
            plugin_registry.unload("nonexistent")
```

### 11.2 Integration Tests

```python
# tests/integration/test_plugin_integration.py

from __future__ import annotations

import asyncio
import pytest
from typing import Any

from agenthicc.kernel import AppState, EventProcessor
from agenthicc.plugins.base import AgenthiccPlugin
from agenthicc.plugins.registry import PluginRegistry, CommandSpec
from agenthicc.tools.base import Tool
from agenthicc.tools.executor import AgenthiccToolExecutor
from agenthicc.tools.hooks import HookRegistry, HookRunner, LifecycleHook


class AddTool(Tool):
    name = "add_numbers"
    description = "Add two numbers."
    parameters = {
        "type": "object",
        "properties": {
            "a": {"type": "number"},
            "b": {"type": "number"},
        },
        "required": ["a", "b"],
    }
    async def execute(self, args: dict, context: dict) -> float:
        return args["a"] + args["b"]


class MathPlugin(AgenthiccPlugin):
    name = "math-plugin"
    version = "1.0.0"
    def on_load(self, registry, config=None):
        registry.register_tool(AddTool())
        registry.register_command(CommandSpec("/add", "Add two numbers"))


@pytest.fixture
def event_processor():
    return EventProcessor(initial_state=AppState.create())


@pytest.fixture
def hook_registry():
    return HookRegistry()


@pytest.fixture
def registry(event_processor, hook_registry):
    return PluginRegistry(event_processor=event_processor, hook_registry=hook_registry)


class TestPluginToolIntegration:
    @pytest.mark.asyncio
    async def test_plugin_tool_callable_via_executor(self, registry, event_processor):
        MathPlugin().on_load(registry)
        tool = registry.get_tool("add_numbers")
        assert tool is not None

        executor = AgenthiccToolExecutor(
            event_processor=event_processor,
            hook_runner=HookRunner(registry=HookRegistry()),
        )
        envelope = await executor.execute(
            tool, {"a": 3.5, "b": 1.5}, {"tool_call_id": "int-1"}
        )
        assert envelope.ok is True
        assert envelope.value == 5.0
        assert envelope.tool_name == "add_numbers"

    @pytest.mark.asyncio
    async def test_plugin_tool_in_appstate_after_load(self, registry, event_processor):
        MathPlugin().on_load(registry)
        await asyncio.sleep(0)
        await event_processor.drain()

        state = event_processor.get_state()
        assert "add_numbers" in state.tools
        assert state.tools["add_numbers"].is_builtin is False

    @pytest.mark.asyncio
    async def test_plugin_hook_fires_on_tool_execute(self, registry, event_processor):
        calls: list[str] = []

        class CountingHook(LifecycleHook):
            async def on_after(self, entity, result, ctx) -> None:
                calls.append(getattr(entity, "name", ""))

        hook_reg = HookRegistry()
        reg2 = PluginRegistry(
            event_processor=event_processor, hook_registry=hook_reg
        )

        class HookPlugin(AgenthiccPlugin):
            name = "hook-plugin"
            version = "0.0.1"
            def on_load(self, registry, config=None):
                registry.register_tool(AddTool())
                registry.register_hook("tool", "after", CountingHook())

        HookPlugin().on_load(reg2)
        tool = reg2.get_tool("add_numbers")
        executor = AgenthiccToolExecutor(
            event_processor=event_processor,
            hook_runner=HookRunner(registry=hook_reg),
        )
        await executor.execute(tool, {"a": 1, "b": 2}, {})
        assert "add_numbers" in calls

    def test_slash_command_available_after_plugin_load(self, registry):
        MathPlugin().on_load(registry)
        spec = registry.get_command("/add")
        assert spec is not None
        assert spec.description == "Add two numbers"
```

### 11.3 E2E Tests

```python
# tests/e2e/test_plugin_e2e.py

from __future__ import annotations

import asyncio
import pytest
from typing import Any

from agenthicc.kernel import AppState, EventProcessor
from agenthicc.plugins.base import AgenthiccPlugin
from agenthicc.plugins.registry import PluginRegistry, PluginManifest
from agenthicc.tools.base import Tool
from agenthicc.tools.executor import AgenthiccToolExecutor
from agenthicc.tools.hooks import HookRegistry, HookRunner, LifecycleHook


class EchoTool(Tool):
    name = "echo_message"
    description = "Echo back a message."
    parameters = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }
    async def execute(self, args: dict, context: dict) -> str:
        return args["message"]


class EchoPlugin(AgenthiccPlugin):
    name = "echo-plugin"
    version = "1.0.0"
    def on_load(self, registry, config=None):
        registry.register_tool(EchoTool())


@pytest.fixture
def event_processor():
    return EventProcessor(initial_state=AppState.create())


@pytest.fixture
def hook_registry():
    return HookRegistry()


@pytest.mark.asyncio
async def test_agent_uses_plugin_tool_via_mock_transport(
    event_processor, hook_registry
):
    """
    Scenario:
      1. EchoPlugin is loaded; echo_message tool registered.
      2. Mock transport emits one tool call: echo_message("hello world").
      3. Executor dispatches the call; plugin tool returns "hello world".
      4. Envelope has ok=True and value="hello world".
    """
    registry = PluginRegistry(
        event_processor=event_processor, hook_registry=hook_registry
    )
    EchoPlugin().on_load(registry)
    await asyncio.sleep(0)
    await event_processor.drain()

    state = event_processor.get_state()
    assert "echo_message" in state.tools

    # Simulate mock transport emitting one tool call.
    tool_call = {"name": "echo_message", "input": {"message": "hello world"}}

    tool = registry.get_tool(tool_call["name"])
    assert tool is not None

    executor = AgenthiccToolExecutor(
        event_processor=event_processor,
        hook_runner=HookRunner(registry=hook_registry),
    )
    envelope = await executor.execute(
        tool, tool_call["input"], {"tool_call_id": "e2e-plugin-01"}
    )
    assert envelope.ok is True
    assert envelope.value == "hello world"
    assert envelope.tool_name == "echo_message"
    assert envelope.duration_ms > 0


@pytest.mark.asyncio
async def test_plugin_unload_makes_tool_unavailable(event_processor, hook_registry):
    """After unload, the tool is no longer in the registry."""
    registry = PluginRegistry(
        event_processor=event_processor, hook_registry=hook_registry
    )
    plugin = EchoPlugin()
    manifest = PluginManifest(
        name=plugin.name, version=plugin.version, entry_point="x:EchoPlugin"
    )
    registry._loaded[plugin.name] = manifest
    registry._plugin_instances[plugin.name] = plugin
    plugin.on_load(registry)
    manifest.status = "loaded"

    assert registry.get_tool("echo_message") is not None
    registry.unload(plugin.name)
    assert registry.get_tool("echo_message") is None


@pytest.mark.asyncio
async def test_plugin_hook_intercepts_tool_call(event_processor, hook_registry):
    """A hook registered by a plugin fires when its tool is invoked."""
    intercepted: list[dict] = []

    class CapturingHook(LifecycleHook):
        async def on_after(self, entity, result, ctx) -> None:
            intercepted.append({
                "tool": getattr(entity, "name", ""), "result": result
            })

    class HookedEchoPlugin(AgenthiccPlugin):
        name = "hooked-echo"
        version = "0.0.1"
        def on_load(self, registry, config=None):
            registry.register_tool(EchoTool())
            registry.register_hook("tool", "after", CapturingHook())

    registry = PluginRegistry(
        event_processor=event_processor, hook_registry=hook_registry
    )
    HookedEchoPlugin().on_load(registry)

    executor = AgenthiccToolExecutor(
        event_processor=event_processor,
        hook_runner=HookRunner(registry=hook_registry),
    )
    tool = registry.get_tool("echo_message")
    envelope = await executor.execute(
        tool, {"message": "test payload"}, {"tool_call_id": "hook-test"}
    )
    assert envelope.ok is True
    assert len(intercepted) == 1
    assert intercepted[0]["tool"] == "echo_message"
    assert intercepted[0]["result"] == "test payload"
```

---

## 12. Open Questions

| # | Question | Owner | Priority | Status |
|---|---|---|---|---|
| OQ-01 | `HookRegistry` does not support hook removal, so unloading a plugin leaves a stale (disconnected) hook entry. Should `HookRegistry.remove(entity_type, stage, hook_instance)` be added as part of this PRD? | Platform | High | Open |
| OQ-02 | `PluginRegistry.discover()` loads all installed plugins unless `[tools].plugins` is set. Should the default be opt-in (only explicitly listed) or opt-out (load all, with a denylist)? | Product | High | Open |
| OQ-03 | Plugin tools bypass the `source_code` compile check from `tool_define`. Should there be a code-signing or package-allowlist gate before a plugin tool can be registered? | Security | High | Open |
| OQ-04 | `_emit_sync` uses `loop.create_task` or `loop.run_until_complete` depending on whether the event loop is running. During synchronous startup, `run_until_complete` blocks. Should `PluginRegistry.discover()` be made async? | Platform | Medium | Open |
| OQ-05 | Skills are copied via `importlib.resources.files()`. For plugins installed as zip archives, this may not return a writable path. Verify compatibility with editable installs and zip-distributed packages. | DX | Medium | Open |
| OQ-06 | Should `on_session_start` / `on_session_end` be invoked by the `EventProcessor` in response to events, or by `AgenthiccApp` directly? | Platform | Low | Open |
| OQ-07 | Plugins that register custom reducer handlers can mutate `AppState` without restriction. Should we provide a read-only projection API for plugins that only need to observe state? | Platform | Medium | Open |
| OQ-08 | Hot reload runs `importlib.reload()` on the module, which replaces module-level singletons. If a plugin holds a module-level connection pool, reload orphans the old pool. Should `on_unload()` be required (not optional)? | DX | Medium | Open |
