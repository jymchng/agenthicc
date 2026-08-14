"""Session-scoped MCP lifecycle and catalog management (PRD-172).

The legacy :class:`~agenthicc.tools.mcp.McpToolRegistry` remains available for
callers that only need a flat list of tools.  This module owns the production
session contract: one bridge per configured server, bounded concurrent
startup, typed status, immutable catalog snapshots, deterministic provider
names, refresh/invalidation, and safe diagnostics.

Protocol details remain in ``lauren_mcp``.  The manager deliberately depends on
the small bridge interface instead of importing an MCP SDK, which keeps unit
tests in-process and lets lauren-ai evolve its transport implementation without
duplicating JSON-RPC code here.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, TYPE_CHECKING, cast

from agenthicc.kernel import Event
from agenthicc.tools.mcp import (
    AgenthiccMcpTool,
    McpServerConfig,
    McpToolBridge,
    McpToolCallError,
    McpStaleCatalogError,
    McpToolSchema,
    _provider_safe_tool_name,
)

if TYPE_CHECKING:
    from agenthicc.kernel.processor import EventProcessor

log = logging.getLogger(__name__)

__all__ = [
    "McpCatalogSnapshot",
    "McpRequiredServerError",
    "McpServerState",
    "McpServerStatus",
    "McpSessionManager",
    "McpStaleCatalogError",
]

_MAX_INSTRUCTION_CHARS = 32_000
_MAX_TOOL_COUNT = 10_000
_MAX_SCHEMA_BYTES = 1_000_000


def _freeze_value(value: object) -> object:
    """Recursively freeze JSON-like protocol data.

    MCP metadata is received from an external process or network peer.  A
    shallow ``MappingProxyType`` is not enough: nested dictionaries and lists
    would still let a provider adapter mutate the catalog after publication.
    JSON arrays become tuples and JSON objects become read-only mappings.  The
    fallback is copied so arbitrary SDK model values cannot retain a mutable
    alias to the transport response.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(_freeze_value(item) for item in value)
    return copy.deepcopy(value)


def _thaw_value(value: object) -> object:
    """Return an independent JSON-compatible mutable representation."""
    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_value(item) for item in value]
    return copy.deepcopy(value)


class McpServerState(StrEnum):
    """Observable lifecycle states for one configured MCP server."""

    CONFIGURED = "configured"
    DISABLED = "disabled"
    STARTING = "starting"
    READY = "ready"
    REFRESHING = "refreshing"
    DEGRADED = "degraded"
    FAILED = "failed"
    AUTHENTICATING = "authenticating"
    NEEDS_AUTH = "needs_auth"
    CANCELLED = "cancelled"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class McpServerStatus:
    """Redacted, serializable status for a configured server."""

    name: str
    state: McpServerState = McpServerState.CONFIGURED
    transport: str = "stdio"
    tool_count: int = 0
    catalog_revision: int = 0
    last_error: str | None = None
    started_at: float | None = None
    last_success_at: float | None = None
    auth_state: str = "not_required"

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.state.value,
            "transport": self.transport,
            "tool_count": self.tool_count,
            "catalog_revision": self.catalog_revision,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "last_success_at": self.last_success_at,
            "auth_state": self.auth_state,
        }


@dataclass(frozen=True, slots=True)
class McpCatalogSnapshot:
    """Immutable tool/instruction snapshot for one server revision."""

    server_name: str
    revision: int
    protocol_version: str = ""
    server_info: Mapping[str, object] = field(default_factory=dict)
    capabilities: Mapping[str, object] = field(default_factory=dict)
    instructions: str = ""
    tools: tuple[McpToolSchema, ...] = ()
    prompts: tuple[object, ...] = ()
    resources: tuple[object, ...] = ()
    tool_filter_hash: str = ""
    catalog_hash: str = ""
    captured_at: float = 0.0

    def __post_init__(self) -> None:
        """Freeze nested protocol values at the snapshot boundary."""
        object.__setattr__(self, "server_info", _freeze_value(self.server_info))
        object.__setattr__(self, "capabilities", _freeze_value(self.capabilities))
        object.__setattr__(
            self,
            "tools",
            tuple(
                McpToolSchema(
                    item.name,
                    item.description,
                    cast(Mapping[str, object], _freeze_value(item.input_schema)),
                )
                for item in self.tools
            ),
        )
        object.__setattr__(self, "prompts", cast(tuple[object, ...], _freeze_value(self.prompts)))
        object.__setattr__(self, "resources", cast(tuple[object, ...], _freeze_value(self.resources)))

    def stable_dict(self) -> dict[str, object]:
        """Return the cache-key representation of this snapshot.

        ``captured_at`` is operational telemetry, not catalog content.  It is
        therefore deliberately excluded: two discoveries that produce the
        same catalog hash must produce byte-for-byte equivalent JSON after
        deterministic serialization.  This is the representation used by
        prompt/cache diagnostics and persisted snapshot implementations.
        """
        return {
            "server_name": self.server_name,
            "revision": self.revision,
            "protocol_version": self.protocol_version,
            "server_info": _thaw_value(self.server_info),
            "capabilities": _thaw_value(self.capabilities),
            "instructions": self.instructions,
            "tools": [
                {
                    "name": item.name,
                    "description": item.description,
                    "input_schema": _thaw_value(item.input_schema),
                }
                for item in self.tools
            ],
            "prompts": _thaw_value(self.prompts),
            "resources": _thaw_value(self.resources),
            "tool_count": len(self.tools),
            "tool_filter_hash": self.tool_filter_hash,
            "catalog_hash": self.catalog_hash,
        }

    def to_dict(self, *, include_capture_time: bool = False) -> dict[str, object]:
        """Return a JSON-safe snapshot representation.

        The default is stable and suitable for cache keys.  Callers that are
        rendering diagnostics can opt into the non-content capture timestamp
        without accidentally making equal catalogs look different to a
        provider cache.
        """
        result = self.stable_dict()
        if include_capture_time:
            result["captured_at"] = self.captured_at
        return result

    def runtime_dict(self) -> dict[str, object]:
        """Return :meth:`to_dict` including operational capture telemetry."""
        return self.to_dict(include_capture_time=True)


class McpRequiredServerError(McpToolCallError):
    """Raised when one or more required servers cannot become ready."""

    def __init__(self, failures: Mapping[str, str]) -> None:
        self.failures = dict(failures)
        super().__init__(
            "required MCP server(s) unavailable: "
            + ", ".join(f"{name}: {error}" for name, error in sorted(self.failures.items()))
        )


BridgeFactory = Callable[[McpServerConfig, "EventProcessor | None"], McpToolBridge]


class McpSessionManager:
    """Own all MCP servers and catalog state for one agenthicc session.

    The manager is intentionally usable with fake bridges.  Tests can inject a
    ``bridge_factory`` or replace ``_bridges[name]`` before startup without
    launching a process or contacting the network.
    """

    def __init__(
        self,
        configs: list[McpServerConfig] | tuple[McpServerConfig, ...] = (),
        *,
        event_processor: "EventProcessor | None" = None,
        workspace_root: Path | None = None,
        bridge_factory: BridgeFactory | None = None,
        network_guard: object | None = None,
        refresh_debounce_s: float = 0.05,
    ) -> None:
        self._events = event_processor
        self._workspace_root = (workspace_root or Path.cwd()).resolve()
        if bridge_factory is not None:
            self._bridge_factory = bridge_factory
        else:
            self._bridge_factory = lambda cfg, events: McpToolBridge(
                cfg,
                events,
                network_guard=network_guard,
                workspace_root=self._workspace_root,
            )
        self._refresh_debounce_s = max(0.0, refresh_debounce_s)
        self._configs: dict[str, McpServerConfig] = {}
        self._bridges: dict[str, McpToolBridge] = {}
        self._snapshots: dict[str, McpCatalogSnapshot] = {}
        self._statuses: dict[str, McpServerStatus] = {}
        self._tools: dict[str, AgenthiccMcpTool] = {}
        self._provider_names: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._lifecycle_tasks: set[asyncio.Task[object]] = set()
        self._revision = 0
        self._closed = False
        self._required_failures: dict[str, str] = {}
        self._registration_errors: dict[str, str] = {}
        for config in configs:
            self.register_server(config)

    @property
    def catalog_revision(self) -> int:
        return self._revision

    @property
    def required_failures(self) -> dict[str, str]:
        return dict(self._required_failures)

    @property
    def bridges(self) -> Mapping[str, McpToolBridge]:
        return MappingProxyType(dict(self._bridges))

    def register_server(self, config: McpServerConfig, *, allow_invalid: bool = False) -> None:
        """Validate and register one server without starting it.

        Normal callers fail fast on malformed configuration.  Session
        construction can pass ``allow_invalid=True`` so an optional malformed
        server is retained as a visible ``failed`` status instead of causing
        otherwise healthy MCP servers (or the whole TUI) to disappear.
        """
        if config.name in self._bridges:
            raise ValueError(f"MCP server {config.name!r} is already registered")
        if config.name in self._configs:
            raise ValueError(f"MCP server {config.name!r} is already registered")
        try:
            config.validate(workspace_root=self._workspace_root)
        except Exception as exc:  # noqa: BLE001
            if not allow_invalid:
                raise
            try:
                error = self._redact(str(exc), config)
            except Exception:  # noqa: BLE001 - validation errors must remain observable
                error = str(exc)[:500]
            self._configs[config.name] = config
            self._locks[config.name] = asyncio.Lock()
            if not config.enabled:
                self._statuses[config.name] = McpServerStatus(
                    name=config.name,
                    state=McpServerState.DISABLED,
                    transport=config.normalized_transport,
                    auth_state="not_required",
                )
                return
            self._registration_errors[config.name] = error
            self._statuses[config.name] = McpServerStatus(
                name=config.name,
                state=McpServerState.FAILED,
                transport=config.normalized_transport,
                last_error=error,
                auth_state=self._auth_state(config),
            )
            if config.required:
                self._required_failures[config.name] = error
            return
        self._configs[config.name] = config
        self._bridges[config.name] = self._bridge_factory(config, self._events)
        self._locks[config.name] = asyncio.Lock()
        auth_state = self._auth_state(config)
        self._statuses[config.name] = McpServerStatus(
            name=config.name,
            transport=config.normalized_transport,
            auth_state=auth_state,
            state=(
                McpServerState.DISABLED
                if not config.enabled
                else (
                    McpServerState.NEEDS_AUTH
                    if auth_state == "needs_auth"
                    else McpServerState.CONFIGURED
                )
            ),
        )

    async def start_all(self, *, raise_required: bool = True) -> list[AgenthiccMcpTool]:
        """Start all eligible servers concurrently and return visible tools."""
        self._closed = False
        names = [
            name
            for name in sorted(self._configs)
            if self._configs[name].auto_connect and name not in self._registration_errors
        ]
        tasks = [
            asyncio.create_task(self.start_server(name), name=f"mcp-start-{name}")
            for name in names
        ]
        self._lifecycle_tasks.update(cast(asyncio.Task[object], task) for task in tasks)
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._lifecycle_tasks.difference_update(
                cast(asyncio.Task[object], task) for task in tasks
            )
        if raise_required and self._required_failures:
            raise McpRequiredServerError(self._required_failures)
        return self.all_tools()

    async def discover_all(self) -> list[AgenthiccMcpTool]:
        """Compatibility spelling used by the original registry."""
        return await self.start_all(raise_required=False)

    async def start_server(self, name: str, *, explicit: bool = False) -> list[AgenthiccMcpTool]:
        """Start one configured server and publish its first catalog snapshot."""
        config = self._require_config(name)
        if name in self._registration_errors:
            if config.required:
                self._required_failures[name] = self._registration_errors[name]
            return []
        if not config.enabled:
            self._set_status(name, state=McpServerState.DISABLED, last_error=None)
            return []
        if not explicit and not config.auto_connect:
            return []
        if self._closed:
            raise McpToolCallError("MCP session manager is shut down")
        async with self._locks[name]:
            if self._statuses[name].state == McpServerState.READY:
                return self._tools_for_server(name)
            self._required_failures.pop(name, None)
            auth_state = self._auth_state(config)
            if auth_state == "needs_auth":
                self._set_status(name, state=McpServerState.NEEDS_AUTH, auth_state=auth_state)
                self._record_failure(name, "authentication required")
                return []
            started_at = time.time()
            self._set_status(
                name,
                state=McpServerState.STARTING,
                last_error=None,
                started_at=started_at,
                auth_state=auth_state,
            )
            bridge = self._bridges[name]
            try:
                await asyncio.wait_for(bridge.connect(), timeout=config.effective_startup_timeout_s)
                schemas = await asyncio.wait_for(
                    bridge.list_tools(), timeout=config.effective_startup_timeout_s
                )
                instructions = await self._optional_bridge_call(bridge, "get_instructions", "")
                capabilities = await self._optional_mapping_call(bridge, "capabilities")
                self._publish(
                    name,
                    schemas,
                    instructions,
                    capabilities,
                    protocol_version=await self._optional_bridge_call(
                        bridge, "protocol_version", ""
                    ),
                    server_info=await self._optional_mapping_call(bridge, "server_info"),
                    prompts=await self._optional_sequence_call(bridge, "list_prompts"),
                    resources=await self._optional_sequence_call(bridge, "list_resources"),
                )
                self._install_change_callback(name, bridge)
                now = time.time()
                snapshot = self._snapshots[name]
                self._set_status(
                    name,
                    state=McpServerState.READY,
                    tool_count=len(snapshot.tools),
                    catalog_revision=snapshot.revision,
                    last_success_at=now,
                    last_error=None,
                    auth_state="authenticated" if auth_state == "authenticated" else auth_state,
                )
                await self._emit(
                    "McpServerReady",
                    {"server": name, "catalog_revision": snapshot.revision, "tool_count": len(snapshot.tools)},
                )
                return self._tools_for_server(name)
            except asyncio.CancelledError:
                self._set_status(name, state=McpServerState.CANCELLED, last_error="startup cancelled")
                raise
            except Exception as exc:  # noqa: BLE001
                error = self._redact(self._bridge_error(bridge, str(exc)), config)
                try:
                    await bridge.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                state = McpServerState.NEEDS_AUTH if _is_auth_error(error) else McpServerState.FAILED
                self._set_status(name, state=state, last_error=error)
                self._record_failure(name, error)
                log.warning("MCP server %r failed to start: %s", name, error)
                return []

    async def connect_server(self, name: str) -> list[AgenthiccMcpTool]:
        task = asyncio.current_task()
        if task is not None:
            self._lifecycle_tasks.add(cast(asyncio.Task[object], task))
        try:
            return await self.start_server(name, explicit=True)
        finally:
            if task is not None:
                self._lifecycle_tasks.discard(cast(asyncio.Task[object], task))

    async def disconnect_server(self, name: str) -> None:
        config = self._require_config(name)
        async with self._locks[name]:
            self._set_status(name, state=McpServerState.STOPPING)
            bridge = self._bridges.get(name)
            if bridge is not None:
                await bridge.disconnect()
            self._remove_server_catalog(name)
            self._set_status(
                name,
                state=McpServerState.DISABLED if not config.enabled else McpServerState.STOPPED,
                tool_count=0,
            )

    async def refresh_server(self, name: str) -> McpCatalogSnapshot | None:
        """Refetch one server and atomically replace its catalog."""
        if name in self._registration_errors or name not in self._bridges:
            return None
        task = asyncio.current_task()
        if task is not None:
            self._lifecycle_tasks.add(cast(asyncio.Task[object], task))
        try:
            config = self._require_config(name)
            if not self._bridges[name].is_connected:
                await self.start_server(name, explicit=True)
                return self._snapshots.get(name)
            async with self._locks[name]:
                bridge = self._bridges[name]
                prior = self._snapshots.get(name)
                self._set_status(name, state=McpServerState.REFRESHING)
                try:
                    schemas = await asyncio.wait_for(
                        bridge.list_tools(), timeout=config.effective_startup_timeout_s
                    )
                    instructions = await self._optional_bridge_call(
                        bridge, "get_instructions", prior.instructions if prior else ""
                    )
                    capabilities = await self._optional_mapping_call(bridge, "capabilities")
                    self._publish(
                        name,
                        schemas,
                        instructions,
                        capabilities,
                        protocol_version=await self._optional_bridge_call(
                            bridge, "protocol_version", prior.protocol_version if prior else ""
                        ),
                        server_info=await self._optional_mapping_call(bridge, "server_info"),
                        prompts=await self._optional_sequence_call(bridge, "list_prompts"),
                        resources=await self._optional_sequence_call(bridge, "list_resources"),
                    )
                    snapshot = self._snapshots[name]
                    self._set_status(
                        name,
                        state=McpServerState.READY,
                        tool_count=len(snapshot.tools),
                        catalog_revision=snapshot.revision,
                        last_success_at=time.time(),
                        last_error=None,
                    )
                    await self._emit(
                        "McpCatalogPublished",
                        {
                            "server": name,
                            "catalog_revision": snapshot.revision,
                            "changed": prior is None
                            or prior.catalog_hash != snapshot.catalog_hash,
                        },
                    )
                    return snapshot
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    error = self._redact(self._bridge_error(bridge, str(exc)), config)
                    if prior is not None:
                        self._set_status(name, state=McpServerState.DEGRADED, last_error=error)
                    else:
                        self._set_status(name, state=McpServerState.FAILED, last_error=error)
                    return prior
        finally:
            if task is not None:
                self._lifecycle_tasks.discard(cast(asyncio.Task[object], task))

    async def notify_tools_changed(self, name: str) -> None:
        """Schedule a debounced refresh; useful for SDK callbacks and tests."""
        if name not in self._bridges or self._closed:
            return
        existing = self._refresh_tasks.get(name)
        if existing is not None and not existing.done():
            return

        async def _refresh() -> None:
            if self._refresh_debounce_s:
                await asyncio.sleep(self._refresh_debounce_s)
            await self.refresh_server(name)

        self._refresh_tasks[name] = asyncio.create_task(_refresh(), name=f"mcp-refresh-{name}")
        try:
            await self._refresh_tasks[name]
        finally:
            self._refresh_tasks.pop(name, None)

    async def shutdown(self) -> None:
        """Cancel refreshes and close every bridge exactly once."""
        if self._closed:
            return
        self._closed = True
        current = asyncio.current_task()
        tasks = {
            task
            for task in (*self._refresh_tasks.values(), *self._lifecycle_tasks)
            if task is not current and not task.done()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        names = sorted(self._bridges)
        await asyncio.gather(*(self._shutdown_one(name) for name in names), return_exceptions=True)
        self._tools.clear()
        self._snapshots.clear()

    async def _shutdown_one(self, name: str) -> None:
        self._set_status(name, state=McpServerState.STOPPING)
        try:
            bridge = self._bridges.get(name)
            if bridge is not None:
                await bridge.disconnect()
        finally:
            self._remove_server_catalog(name)
            self._set_status(name, state=McpServerState.STOPPED, tool_count=0)

    def get_tool(self, name: str) -> AgenthiccMcpTool | None:
        return self._tools.get(name) or next(
            (tool for tool in self._tools.values() if tool.provider_name == name), None
        )

    def all_tools(self) -> list[AgenthiccMcpTool]:
        return [self._tools[name] for name in sorted(self._tools)]

    def status(self, name: str | None = None) -> dict[str, dict[str, object]] | dict[str, object]:
        if name is not None:
            return self._require_status(name).to_dict()
        return {key: self._statuses[key].to_dict() for key in sorted(self._statuses)}

    def snapshots(self) -> Mapping[str, McpCatalogSnapshot]:
        return MappingProxyType(dict(self._snapshots))

    def catalog(self) -> list[dict[str, object]]:
        """Return the stable model-facing catalog plus canonical identity."""
        return [
            {
                "name": tool.provider_name,
                "canonical_name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "server": tool._bridge.server_name,
                "catalog_revision": tool.catalog_revision,
            }
            for tool in self.all_tools()
        ]

    def instructions(self) -> list[dict[str, object]]:
        return [
            {
                "server": name,
                "instructions": snapshot.instructions,
                "tools": [tool.name for tool in snapshot.tools],
                "catalog_revision": snapshot.revision,
            }
            for name, snapshot in sorted(self._snapshots.items())
            if snapshot.instructions and self._statuses[name].state in {McpServerState.READY, McpServerState.DEGRADED}
        ]

    def prompt_instructions(self) -> str:
        """Return the cache-stable, provenance-labeled MCP prompt block.

        MCP instructions are server-supplied metadata, not policy. The label
        is deliberately explicit so a model cannot mistake them for host
        instructions. The block changes only when a published catalog changes.
        """
        blocks = []
        for item in self.instructions():
            blocks.append(
                "Instructions supplied by MCP server "
                f"{item['server']!r} (untrusted metadata; catalog revision "
                f"{item['catalog_revision']}):\n{item['instructions']}"
            )
        return "\n\n".join(blocks)

    def redacted_config(self) -> list[dict[str, object]]:
        return [self._configs[name].redacted() for name in sorted(self._configs)]

    async def doctor(self, name: str | None = None) -> list[dict[str, object]]:
        """Validate and optionally live-discover servers without invoking tools."""
        names = [name] if name else sorted(self._configs)
        results: list[dict[str, object]] = []
        for server_name in names:
            config = self._require_config(server_name)
            item: dict[str, object] = {"server": server_name, "config": config.redacted()}
            try:
                config.validate(workspace_root=self._workspace_root)
                if config.enabled and config.auto_connect:
                    await self.start_server(server_name, explicit=True)
                item["status"] = self.status(server_name)
            except Exception as exc:  # noqa: BLE001
                item["status"] = "failed"
                item["error"] = self._redact(str(exc), config)
            results.append(item)
        return results

    def _publish(
        self,
        name: str,
        schemas: list[McpToolSchema],
        instructions: str,
        capabilities: Mapping[str, object],
        *,
        protocol_version: str = "",
        server_info: Mapping[str, object] | None = None,
        prompts: tuple[object, ...] = (),
        resources: tuple[object, ...] = (),
    ) -> None:
        config = self._configs[name]
        if len(schemas) > _MAX_TOOL_COUNT:
            raise McpToolCallError(f"MCP server {name!r} returned too many tools")
        normalized: list[McpToolSchema] = []
        seen_names: set[str] = set()
        for schema in schemas:
            tool_name = str(schema.name).strip()
            if not tool_name:
                raise McpToolCallError(f"MCP server {name!r} returned a tool without a name")
            if tool_name in seen_names:
                raise McpToolCallError(
                    f"MCP server {name!r} returned duplicate tool {tool_name!r}"
                )
            if not isinstance(schema.input_schema, Mapping):
                raise McpToolCallError(f"MCP tool {tool_name!r} returned an invalid input schema")
            copied_schema = copy.deepcopy(dict(schema.input_schema))
            try:
                schema_size = len(json.dumps(copied_schema, default=str).encode())
            except (TypeError, ValueError) as exc:
                raise McpToolCallError(
                    f"MCP tool {tool_name!r} returned an unserializable schema"
                ) from exc
            if schema_size > _MAX_SCHEMA_BYTES:
                raise McpToolCallError(
                    f"MCP tool {tool_name!r} returned an oversized input schema"
                )
            seen_names.add(tool_name)
            normalized.append(
                McpToolSchema(
                    tool_name,
                    str(schema.description or ""),
                    cast(Mapping[str, object], _freeze_value(copied_schema)),
                )
            )
        visible = [
            schema
            for schema in normalized
            if (not config.enabled_tools or schema.name in config.enabled_tools)
            and schema.name not in config.disabled_tools
        ]
        visible.sort(key=lambda schema: schema.name)
        instructions = str(instructions or "").strip()[:_MAX_INSTRUCTION_CHARS]
        safe_server_info = dict(server_info or {})
        canonical = {
            "server": name,
            "tools": [
                {
                    "name": item.name,
                    "description": item.description,
                    "input_schema": _thaw_value(item.input_schema),
                }
                for item in visible
            ],
            "instructions": instructions,
            "capabilities": _thaw_value(capabilities),
            "protocol_version": protocol_version,
            "server_info": _thaw_value(safe_server_info),
            "prompts": _thaw_value(prompts),
            "resources": _thaw_value(resources),
            "enabled_tools": list(config.enabled_tools),
            "disabled_tools": list(config.disabled_tools),
        }
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        previous = self._snapshots.get(name)
        revision = previous.revision if previous and previous.catalog_hash == digest else self._revision + 1
        self._revision = max(self._revision, revision)
        snapshot = McpCatalogSnapshot(
            server_name=name,
            revision=revision,
            protocol_version=protocol_version,
            server_info=cast(Mapping[str, object], _freeze_value(safe_server_info)),
            instructions=instructions,
            capabilities=cast(Mapping[str, object], _freeze_value(dict(capabilities))),
            tools=tuple(visible),
            prompts=cast(tuple[object, ...], _freeze_value(prompts)),
            resources=cast(tuple[object, ...], _freeze_value(resources)),
            tool_filter_hash=hashlib.sha256(
                json.dumps([config.enabled_tools, config.disabled_tools], default=str).encode()
            ).hexdigest()[:16],
            catalog_hash=digest,
            captured_at=time.time(),
        )
        # Build all provider names before mutating the shared surface. A
        # collision therefore fails the candidate without deleting last-good
        # tools from any server.
        candidate_tools = dict(self._tools)
        candidate_provider = dict(self._provider_names)
        for tool_name, tool in list(candidate_tools.items()):
            if tool._bridge.server_name == name:
                candidate_tools.pop(tool_name, None)
                candidate_provider.pop(tool.provider_name, None)
        for schema in snapshot.tools:
            tool = AgenthiccMcpTool(
                self._bridges[name],
                schema,
                catalog_revision=revision,
                catalog_revision_checker=self._is_catalog_binding_current,
            )
            provider_name = _provider_safe_tool_name(tool.name)
            owner = candidate_provider.get(provider_name)
            if owner is not None and owner != tool.name:
                provider_name = _provider_safe_tool_name(tool.name + "#" + digest[:12])
            if provider_name in candidate_provider and candidate_provider[provider_name] != tool.name:
                raise McpToolCallError(f"provider tool name collision for {tool.name!r}")
            tool._provider_name_override = provider_name
            candidate_tools[tool.name] = tool
            candidate_provider[provider_name] = tool.name
        self._snapshots[name] = snapshot
        self._tools = candidate_tools
        self._provider_names = candidate_provider

    def _is_catalog_binding_current(self, canonical_name: str, revision: int) -> bool:
        tool = self._tools.get(canonical_name)
        return tool is not None and tool.catalog_revision == revision

    def _remove_server_catalog(self, name: str) -> None:
        for canonical, tool in list(self._tools.items()):
            if tool._bridge.server_name == name:
                self._provider_names.pop(tool.provider_name, None)
                self._tools.pop(canonical, None)
        self._snapshots.pop(name, None)

    def _tools_for_server(self, name: str) -> list[AgenthiccMcpTool]:
        return [tool for tool in self.all_tools() if tool._bridge.server_name == name]

    def _install_change_callback(self, name: str, bridge: McpToolBridge) -> None:
        callback = getattr(bridge, "set_change_callback", None)
        if callable(callback):
            async def _changed(*_args: object, **_kwargs: object) -> None:
                await self.notify_tools_changed(name)

            callback(_changed)

    async def _emit(self, event_type: str, payload: dict[str, object]) -> None:
        if self._events is None:
            return
        await self._events.emit(Event.create(event_type, payload))

    def _record_failure(self, name: str, error: str) -> None:
        if self._configs[name].required:
            self._required_failures[name] = error

    def _set_status(self, name: str, **changes: object) -> None:
        current = self._require_status(name)
        # ``dataclasses.replace`` cannot express a heterogeneous keyword map
        # in mypy, while this helper is intentionally the single dynamic
        # update boundary for the frozen status record.
        self._statuses[name] = replace(current, **cast(Any, changes))

    def _require_config(self, name: str) -> McpServerConfig:
        try:
            return self._configs[name]
        except KeyError as exc:
            raise KeyError(f"No MCP server registered with name {name!r}") from exc

    def _require_status(self, name: str) -> McpServerStatus:
        self._require_config(name)
        return self._statuses[name]

    @staticmethod
    async def _optional_bridge_call(bridge: object, method_name: str, default: str) -> str:
        value = getattr(bridge, method_name, None)
        if value is None:
            return default
        if callable(value):
            value = value()
        if inspect.isawaitable(value):
            value = await value
        return value if isinstance(value, str) else default

    @staticmethod
    async def _optional_mapping_call(bridge: object, method_name: str) -> dict[str, object]:
        value = getattr(bridge, method_name, None)
        if value is None:
            return {}
        if callable(value):
            value = value()
        if inspect.isawaitable(value):
            value = await value
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    async def _optional_sequence_call(bridge: object, method_name: str) -> tuple[object, ...]:
        value = getattr(bridge, method_name, None)
        if value is None:
            return ()
        if callable(value):
            value = value()
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, (list, tuple)):
            return ()
        return tuple(copy.deepcopy(item) for item in value)

    @staticmethod
    def _auth_state(config: McpServerConfig) -> str:
        required = list(config.env_headers.values())
        if config.token.startswith("${"):
            required.append(config.token[2:-1])
        missing = [name for name in required if name and not os.environ.get(name)]
        if missing:
            return "needs_auth"
        if required or config.oauth:
            return "authenticated" if required and not missing else "needs_auth"
        return "not_required"

    @staticmethod
    def _redact(error: str, config: McpServerConfig) -> str:
        result = error
        secret_env = [
            value
            for key, value in config.resolved_env().items()
            if key and any(token in key.upper() for token in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTH"))
        ]
        for value in (
            config.resolved_token(),
            *config.resolved_headers().values(),
            *secret_env,
            *config.resolved_command(),
        ):
            if value and len(value) >= 4:
                result = result.replace(value, "<redacted>")
        return result

    @staticmethod
    def _bridge_error(bridge: object, error: str) -> str:
        """Add bounded stdio diagnostics without treating them as protocol."""
        stderr = getattr(bridge, "stderr_tail", "")
        if isinstance(stderr, str) and stderr.strip():
            return f"{error}; stderr: {stderr[-4096:]}"
        return error


def _is_auth_error(error: str) -> bool:
    value = error.lower()
    return any(token in value for token in ("401", "403", "unauthorized", "authentication", "auth required"))
