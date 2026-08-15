"""Unit coverage for the PRD-172 MCP session manager."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import pytest

from agenthicc.tools.mcp import McpServerConfig, McpStaleCatalogError, McpToolSchema
from agenthicc.tools.mcp_manager import (
    McpRequiredServerError,
    McpServerState,
    McpSessionManager,
)

pytestmark = pytest.mark.unit


@dataclass
class FakeBridge:
    config: McpServerConfig
    delay: float = 0.0
    fail: str | None = None

    def __post_init__(self) -> None:
        self.server_name = self.config.name
        self.is_connected = False
        self.tools = [McpToolSchema("ping", "Ping", {"type": "object"})]
        self.instructions = "Use ping only."
        self.callback = None
        self.disconnect_count = 0
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def connect(self) -> None:
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError(self.fail)
        self.is_connected = True

    async def disconnect(self) -> None:
        self.disconnect_count += 1
        self.is_connected = False

    async def list_tools(self) -> list[McpToolSchema]:
        if self.fail and self.is_connected:
            raise RuntimeError(self.fail)
        return list(self.tools)

    async def get_instructions(self) -> str:
        return self.instructions

    async def capabilities(self) -> dict[str, object]:
        return {"tools": {"listChanged": True}}

    @property
    def protocol_version(self) -> str:
        return "2025-06-18"

    @property
    def server_info(self) -> dict[str, object]:
        return {"name": self.server_name, "version": "1"}

    async def list_prompts(self) -> list[object]:
        return [{"name": "prompt"}]

    async def list_resources(self) -> list[object]:
        return [{"uri": "memory://resource"}]

    async def call_tool(
        self, name: str, arguments: dict[str, object], *, tool_call_id: str = ""
    ) -> dict[str, object]:
        self.calls.append((name, dict(arguments)))
        return {"ok": True, "tool_call_id": tool_call_id}

    def set_change_callback(self, callback: object) -> None:
        self.callback = callback


def _factory(bridges: dict[str, FakeBridge]):
    def build(config: McpServerConfig, _events: object) -> FakeBridge:
        bridge = FakeBridge(config)
        bridges[config.name] = bridge
        return bridge

    return build


def _config(name: str, **kwargs: object) -> McpServerConfig:
    return McpServerConfig(name=name, url="fake-server", **kwargs)


def test_config_supports_argv_and_redacts_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TOKEN", "secret-token")
    config = McpServerConfig.from_dict(
        {
            "name": "remote",
            "transport": "streamable_http",
            "url": "https://example.test/mcp",
            "command": ["ignored"],
            "env_headers": {"Authorization": "MCP_TOKEN"},
            "required": True,
        }
    )
    config.validate()
    assert config.normalized_transport == "streamable_http"
    assert config.resolved_headers()["Authorization"] == "secret-token"
    assert "secret-token" not in str(config.redacted())


def test_redacted_config_hides_secret_command_arguments_and_url_query() -> None:
    config = McpServerConfig(
        name="safe",
        command=("server", "--api-key=super-secret"),
        url="https://example.test/mcp?api_key=super-secret&project=demo",
        transport="streamable_http",
    )
    redacted = config.redacted()
    assert "super-secret" not in str(redacted)
    assert "project=demo" in str(redacted["url"])


def test_strict_config_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="unknown MCP configuration"):
        McpServerConfig.from_dict({"name": "x", "url": "fake", "typo": True}, strict=True)


@pytest.mark.asyncio
async def test_optional_servers_start_concurrently() -> None:
    bridges: dict[str, FakeBridge] = {}
    manager = McpSessionManager(
        [_config("slow"), _config("fast")],
        bridge_factory=_factory(bridges),
    )
    # Replace factory-created delay after registration without changing config.
    bridges["slow"].delay = 0.08
    bridges["fast"].delay = 0.08
    started = time.monotonic()
    await manager.start_all()
    elapsed = time.monotonic() - started
    assert elapsed < 0.14
    assert all(item["status"] == "ready" for item in manager.status().values())
    await manager.shutdown()


@pytest.mark.asyncio
async def test_required_failure_isolated_and_reported() -> None:
    bridges: dict[str, FakeBridge] = {}
    manager = McpSessionManager(
        [_config("required", required=True), _config("optional")],
        bridge_factory=_factory(bridges),
    )
    bridges["required"].fail = "connection refused"
    with pytest.raises(McpRequiredServerError) as exc_info:
        await manager.start_all()
    assert "required" in exc_info.value.failures
    assert manager.status("required")["status"] == McpServerState.FAILED.value
    assert manager.status("optional")["status"] == McpServerState.READY.value
    await manager.shutdown()


@pytest.mark.asyncio
async def test_catalog_is_filtered_stable_and_contains_instructions() -> None:
    bridges: dict[str, FakeBridge] = {}
    config = _config("server", enabled_tools=("ping",))
    manager = McpSessionManager([config], bridge_factory=_factory(bridges))
    await manager.start_all()
    snapshot = manager.snapshots()["server"]
    assert snapshot.revision == 1
    assert snapshot.catalog_hash
    assert manager.catalog()[0]["name"] == "mcp_server_ping"
    assert manager.instructions()[0]["instructions"] == "Use ping only."
    assert snapshot.protocol_version == "2025-06-18"
    assert snapshot.server_info["name"] == "server"
    assert snapshot.prompts == ({"name": "prompt"},)
    assert snapshot.resources == ({"uri": "memory://resource"},)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_catalog_snapshot_freezes_nested_metadata_and_has_stable_serialization() -> None:
    bridges: dict[str, FakeBridge] = {}
    manager = McpSessionManager([_config("server")], bridge_factory=_factory(bridges))
    await manager.start_all()
    snapshot = manager.snapshots()["server"]

    with pytest.raises(TypeError):
        snapshot.tools[0].input_schema["properties"] = {}  # type: ignore[index]

    first = snapshot.to_dict()
    second = snapshot.to_dict()
    assert first == second
    assert "captured_at" not in first
    assert snapshot.runtime_dict()["captured_at"] == snapshot.captured_at
    assert manager.catalog()[0]["parameters"] == {"type": "object"}
    await manager.shutdown()


@pytest.mark.asyncio
async def test_invalid_optional_server_remains_visible_without_blocking_valid_server() -> None:
    bridges: dict[str, FakeBridge] = {}
    manager = McpSessionManager(bridge_factory=_factory(bridges))
    manager.register_server(
        McpServerConfig(name="invalid", transport="streamable_http", url="not-a-url"),
        allow_invalid=True,
    )
    manager.register_server(_config("valid"), allow_invalid=True)
    await manager.start_all()
    assert manager.status("invalid")["status"] == McpServerState.FAILED.value
    assert manager.status("valid")["status"] == McpServerState.READY.value
    await manager.shutdown()


@pytest.mark.asyncio
async def test_disabled_server_is_visible_but_never_started() -> None:
    bridges: dict[str, FakeBridge] = {}
    manager = McpSessionManager(
        [_config("disabled", enabled=False)], bridge_factory=_factory(bridges)
    )
    await manager.start_all()
    assert manager.status("disabled")["status"] == McpServerState.DISABLED.value
    assert bridges["disabled"].is_connected is False
    assert manager.all_tools() == []
    await manager.shutdown()


@pytest.mark.asyncio
async def test_old_tool_binding_fails_after_catalog_replacement() -> None:
    bridges: dict[str, FakeBridge] = {}
    manager = McpSessionManager([_config("server")], bridge_factory=_factory(bridges))
    await manager.start_all()
    old_tool = manager.get_tool("mcp:server:ping")
    assert old_tool is not None
    bridges["server"].tools = [McpToolSchema("new", "New", {"type": "object"})]
    await manager.refresh_server("server")
    with pytest.raises(McpStaleCatalogError, match="stale catalog"):
        await old_tool.execute({}, {})
    await manager.shutdown()


@pytest.mark.asyncio
async def test_refresh_replaces_removed_tools_and_increments_revision() -> None:
    bridges: dict[str, FakeBridge] = {}
    manager = McpSessionManager([_config("server")], bridge_factory=_factory(bridges))
    await manager.start_all()
    first_revision = manager.snapshots()["server"].revision
    bridges["server"].tools = [McpToolSchema("new_tool", "New", {"type": "object"})]
    refreshed = await manager.refresh_server("server")
    assert refreshed is not None
    assert refreshed.revision > first_revision
    assert manager.get_tool("mcp:server:ping") is None
    assert manager.get_tool("mcp:server:new_tool") is not None
    await manager.shutdown()


@pytest.mark.asyncio
async def test_reload_all_reconnects_every_enabled_server_and_republishes_catalogs() -> None:
    bridges: dict[str, FakeBridge] = {}
    manager = McpSessionManager(
        [_config("automatic"), _config("manual", auto_connect=False), _config("disabled", enabled=False)],
        bridge_factory=_factory(bridges),
    )
    await manager.start_all()
    assert manager.get_tool("mcp:automatic:ping") is not None
    assert manager.get_tool("mcp:manual:ping") is None

    bridges["automatic"].tools = [McpToolSchema("new", "New", {"type": "object"})]
    bridges["manual"].tools = [McpToolSchema("manual_tool", "Manual", {"type": "object"})]
    await manager.reload_all()

    assert manager.get_tool("mcp:automatic:ping") is None
    assert manager.get_tool("mcp:automatic:new") is not None
    assert manager.get_tool("mcp:manual:manual_tool") is not None
    assert manager.status("automatic")["status"] == McpServerState.READY.value
    assert manager.status("manual")["status"] == McpServerState.READY.value
    assert manager.status("disabled")["status"] == McpServerState.DISABLED.value
    assert bridges["automatic"].disconnect_count == 1
    await manager.shutdown()


@pytest.mark.asyncio
async def test_reload_all_isolates_optional_failure_and_reports_required_failure() -> None:
    bridges: dict[str, FakeBridge] = {}
    manager = McpSessionManager(
        [_config("required", required=True), _config("optional")],
        bridge_factory=_factory(bridges),
    )
    await manager.start_all()
    bridges["required"].fail = "required unavailable"
    bridges["optional"].tools = [McpToolSchema("still_works", "Works", {"type": "object"})]

    with pytest.raises(McpRequiredServerError, match="required"):
        await manager.reload_all()
    assert manager.status("required")["status"] == McpServerState.FAILED.value
    assert manager.status("optional")["status"] == McpServerState.READY.value
    assert manager.get_tool("mcp:optional:still_works") is not None
    await manager.shutdown()


@pytest.mark.asyncio
async def test_failed_refresh_retains_last_good_catalog() -> None:
    bridges: dict[str, FakeBridge] = {}
    manager = McpSessionManager([_config("server")], bridge_factory=_factory(bridges))
    await manager.start_all()
    previous = manager.snapshots()["server"]
    bridges["server"].fail = "temporary failure"
    result = await manager.refresh_server("server")
    assert result == previous
    assert manager.get_tool("mcp:server:ping") is not None
    assert manager.status("server")["status"] == McpServerState.DEGRADED.value
    await manager.shutdown()


@pytest.mark.asyncio
async def test_shutdown_is_idempotent_and_clears_visible_tools() -> None:
    bridges: dict[str, FakeBridge] = {}
    manager = McpSessionManager([_config("server")], bridge_factory=_factory(bridges))
    await manager.start_all()
    await manager.shutdown()
    await manager.shutdown()
    assert manager.all_tools() == []
    assert manager.status("server")["status"] == McpServerState.STOPPED.value
    assert manager.status("server")["tool_count"] == 0
    assert bridges["server"].disconnect_count == 1
