"""Unit coverage for the PRD-172 MCP session manager."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import pytest

from agenthicc.tools.mcp import McpStaleCatalogError, McpToolSchema
from agenthicc.tools.mcp_availability import McpClientStatus, McpUnavailable

from agenthicc.tools.mcp_server_config import McpServerConfig
from agenthicc.tools.mcp_manager import (
    McpRequiredServerError,
    McpServerState,
    McpSessionManager,
)
from agenthicc.tui.conversation_store import ConversationStore

pytestmark = pytest.mark.unit


@dataclass
class FakeBridge:
    config: McpServerConfig
    delay: float = 0.0
    fail: str | None = None
    unavailable: McpClientStatus | None = None

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
        if self.unavailable is not None:
            raise McpUnavailable(self.unavailable)
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

    async def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, dict(arguments)))
        return {"ok": True}

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


class EventRecorder:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)


def test_required_auto_connect_servers_excludes_optional_and_manual_servers() -> None:
    manager = McpSessionManager(
        [
            _config("required", required=True),
            _config("optional"),
            _config("manual", required=True, auto_connect=False),
            _config("disabled", required=True, enabled=False),
        ]
    )

    assert manager.required_auto_connect_servers == ("required",)


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
async def test_startup_generation_is_attached_to_one_optional_failure_event() -> None:
    bridges: dict[str, FakeBridge] = {}
    events = EventRecorder()
    manager = McpSessionManager(
        [_config("asyncmove"), _config("healthy")],
        event_processor=events,  # type: ignore[arg-type]
        bridge_factory=_factory(bridges),
    )
    bridges["asyncmove"].fail = "connection refused"

    await manager.start_all()

    failures = [
        event for event in events.events if getattr(event, "event_type", "") == "McpServerFailed"
    ]
    assert len(failures) == 1
    assert failures[0].payload["server"] == "asyncmove"  # type: ignore[attr-defined]
    assert failures[0].payload["generation_id"] == 1  # type: ignore[attr-defined]
    assert failures[0].payload["event_id"]  # type: ignore[attr-defined]
    assert failures[0].payload["status"] == "failed_optional"  # type: ignore[attr-defined]
    assert manager.status("healthy")["status"] == McpServerState.READY.value
    assert manager.status("asyncmove")["generation_id"] == 1
    await manager.shutdown()


@pytest.mark.asyncio
async def test_optional_failure_is_projected_once_into_the_tui_store() -> None:
    bridges: dict[str, FakeBridge] = {}
    store = ConversationStore()
    projected: list[object] = []
    store.on_event(projected.append)
    manager = McpSessionManager(
        [_config("asyncmove")],
        conversation_store=store,
        bridge_factory=_factory(bridges),
    )
    bridges["asyncmove"].fail = "connection refused"

    await manager.start_all()
    await manager.start_all()

    failures = [event for event in projected if getattr(event, "kind", "") == "mcp_server_failure"]
    assert len(failures) == 2
    assert failures[0].payload["server"] == "asyncmove"  # type: ignore[attr-defined]
    assert failures[0].payload["generation_id"] == 1  # type: ignore[attr-defined]
    assert failures[1].payload["generation_id"] == 2  # type: ignore[attr-defined]
    await manager.shutdown()


@pytest.mark.asyncio
async def test_the_status_badge_counts_enabled_servers_that_failed_or_need_sign_in() -> None:
    """MCP-01 D3: ``mcp_attention`` follows the statuses, never a stale count."""
    bridges: dict[str, FakeBridge] = {}
    store = ConversationStore()
    manager = McpSessionManager(
        [_config("good"), _config("bad"), _config("off", enabled=False)],
        conversation_store=store,
        bridge_factory=_factory(bridges),
    )
    assert store.mcp_attention() == 0  # configured, not yet started

    bridges["bad"].fail = "connection refused"
    await manager.start_all()
    assert store.mcp_attention() == 1  # the disabled server is not counted

    bridges["bad"].fail = None
    await manager.start_server("bad")
    assert store.mcp_attention() == 0  # recovery clears it
    await manager.shutdown()


@pytest.mark.asyncio
async def test_a_missing_client_raises_the_badge_for_every_server_but_one_notice() -> None:
    bridges: dict[str, FakeBridge] = {}
    store = ConversationStore()
    projected: list[object] = []
    store.on_event(projected.append)
    manager = McpSessionManager(
        [_config("one"), _config("two")],
        conversation_store=store,
        bridge_factory=_factory(bridges),
    )
    missing = McpClientStatus(False, "dependency_missing", fix="python -m pip install x")
    for bridge in bridges.values():
        bridge.unavailable = missing
    await manager.start_all()
    assert store.mcp_attention() == 2
    notices = [e for e in projected if getattr(e, "kind", "") == "mcp_server_failure"]
    assert len(notices) == 1
    await manager.shutdown()


@pytest.mark.asyncio
async def test_stale_startup_generation_cannot_publish_catalog() -> None:
    bridges: dict[str, FakeBridge] = {}
    manager = McpSessionManager([_config("slow")], bridge_factory=_factory(bridges))
    bridges["slow"].delay = 0.05
    generation = manager._begin_generation()
    task = asyncio.create_task(manager.start_server("slow", _generation_id=generation))
    await asyncio.sleep(0.005)
    manager._begin_generation()
    await task

    assert manager.all_tools() == []
    assert manager.status("slow")["status"] != McpServerState.READY.value
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
    assert manager.catalog()[0]["parameters"] == {"type": "object", "properties": {}}
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
        [
            _config("automatic"),
            _config("manual", auto_connect=False),
            _config("disabled", enabled=False),
        ],
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


# ── MCP-01: an unusable client library is one cause, reported once, with its fix ──────────

_FIX = "python -m pip install 'lauren-mcp[http,ws]>=1.2.1,<2'"


def _unavailable(reason: str = "dependency_missing", detail: str = "") -> McpClientStatus:
    installed = "" if reason == "dependency_missing" else "1.1.0"
    return McpClientStatus(False, reason, installed=installed, detail=detail, fix=_FIX)


def _failure_notices(projected: list[object]) -> list[dict[str, object]]:
    return [
        dict(getattr(event, "payload", {}))
        for event in projected
        if getattr(event, "kind", "") == "mcp_server_failure"
    ]


@pytest.mark.asyncio
async def test_a_missing_client_is_one_notice_naming_the_cause_and_the_fix() -> None:
    bridges: dict[str, FakeBridge] = {}
    store = ConversationStore()
    projected: list[object] = []
    store.on_event(projected.append)
    manager = McpSessionManager(
        [_config("a"), _config("b"), _config("c")],
        conversation_store=store,
        bridge_factory=_factory(bridges),
    )
    for bridge in bridges.values():
        bridge.unavailable = _unavailable()

    await manager.start_all()

    (notice,) = _failure_notices(projected)
    assert "lauren-mcp is not installed" in str(notice["text"])  # the cause, on the same line
    assert _FIX in str(notice["body"])
    for name in ("a", "b", "c"):
        status = manager.status(name)
        assert status["status"] == McpServerState.FAILED.value
        assert status["client_available"] is False
        assert status["reason"] == "dependency_missing"
        assert status["fix"] == _FIX
        assert "lauren-mcp is not installed" in str(status["last_error"])
    await manager.shutdown()


@pytest.mark.asyncio
async def test_a_missing_client_stops_startup_when_a_server_is_required() -> None:
    bridges: dict[str, FakeBridge] = {}
    store = ConversationStore()
    projected: list[object] = []
    store.on_event(projected.append)
    manager = McpSessionManager(
        [_config("needed", required=True), _config("extra")],
        conversation_store=store,
        bridge_factory=_factory(bridges),
    )
    for bridge in bridges.values():
        bridge.unavailable = _unavailable()

    with pytest.raises(McpRequiredServerError) as raised:
        await manager.start_all()

    assert "lauren-mcp is not installed" in raised.value.failures["needed"]
    notices = _failure_notices(projected)
    assert 1 <= len(notices) <= 2  # a warning for the optional server, an error for the required
    assert any(notice["required"] is True for notice in notices)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_the_cause_is_reported_again_for_each_startup_and_cleared_once_fixed() -> None:
    bridges: dict[str, FakeBridge] = {}
    store = ConversationStore()
    projected: list[object] = []
    store.on_event(projected.append)
    manager = McpSessionManager(
        [_config("a")], conversation_store=store, bridge_factory=_factory(bridges)
    )
    bridges["a"].unavailable = _unavailable()

    await manager.start_all()
    await manager.reload_all()
    assert len(_failure_notices(projected)) == 2  # one per startup generation, not one forever

    bridges["a"].unavailable = None  # the operator installed the client
    await manager.reload_all()

    status = manager.status("a")
    assert status["status"] == McpServerState.READY.value
    assert status["client_available"] is True
    assert status["reason"] == "" and status["fix"] == ""
    assert len(_failure_notices(projected)) == 2
    await manager.shutdown()


@pytest.mark.asyncio
async def test_an_unusable_client_is_never_reported_as_an_authentication_failure() -> None:
    bridges: dict[str, FakeBridge] = {}
    manager = McpSessionManager([_config("a")], bridge_factory=_factory(bridges))
    # The cause text contains "401", which the auth-error text match treats as an auth failure.
    bridges["a"].unavailable = _unavailable("dependency_broken", "ImportError: error 401")

    await manager.start_all()

    assert manager.status("a")["status"] == McpServerState.FAILED.value
    await manager.shutdown()


@pytest.mark.asyncio
async def test_diagnostics_puts_the_client_check_before_the_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridges: dict[str, FakeBridge] = {}
    manager = McpSessionManager([_config("a")], bridge_factory=_factory(bridges))
    monkeypatch.setattr("agenthicc.tools.mcp_manager.client_status", lambda: _unavailable())

    result = await manager.diagnostics()

    assert list(result) == ["client", "servers"]
    client = result["client"]
    assert isinstance(client, dict)
    assert client["client_available"] is False
    assert client["reason"] == "dependency_missing"
    assert client["fix"] == _FIX
    servers = result["servers"]
    assert isinstance(servers, list) and servers[0]["server"] == "a"
    await manager.shutdown()
