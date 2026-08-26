"""Unit coverage for the PRD-174 authoring catalog and provenance contract."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agenthicc.tools.capabilities import (
    ToolCapability,
    tool_exploratory,
    tool_read_search,
    tool_write,
)
from agenthicc.tui.conversation_store import AppState
from agenthicc.workflows.create_workflow.catalog import (
    AUTHORING_CATALOG_VERSION,
    AuthoringSnapshot,
    AuthoringSnapshotCache,
    build_authoring_snapshot,
    build_tool_catalog,
    explain_tool_access,
)
from agenthicc.workflows.create_workflow.inspection_tools import make_inspection_tools
from lauren_ai._tools import tool

pytestmark = pytest.mark.unit


@tool_exploratory
@tool_read_search
@tool()
async def inspect_document(path: str, max_lines: int = 20) -> dict[str, object]:
    """Read a bounded document."""
    return {"path": path, "max_lines": max_lines}


@tool_write
@tool()
async def write_document(path: str, content: str) -> dict[str, object]:
    """Write a document."""
    return {"path": path, "content": content}


def test_catalog_uses_live_schema_capabilities_and_presentation_metadata() -> None:
    entries = build_tool_catalog([write_document, inspect_document])
    assert [entry.name for entry in entries] == ["inspect_document", "write_document"]

    inspect_entry = entries[0]
    assert set(inspect_entry.capabilities) == {"read", "search"}
    assert inspect_entry.exploratory is True
    assert inspect_entry.source == "plugin"
    assert inspect_entry.parameters
    assert inspect_entry.fingerprint

    write_entry = entries[1]
    assert write_entry.capabilities == ("write",)
    assert write_entry.exploratory is False


def test_catalog_applies_mode_and_phase_decisions_fail_closed() -> None:
    mode = SimpleNamespace(blocked_capabilities=frozenset({ToolCapability.WRITE}))
    entries = build_tool_catalog(
        [write_document, inspect_document],
        active_mode=mode,
        phase_capabilities=(ToolCapability.READ, ToolCapability.SEARCH),
    )
    write_entry, inspect_entry = entries[1], entries[0]
    assert write_entry.available is False
    assert write_entry.availability_reason == "active_mode_blocks_capability"
    assert inspect_entry.available is True

    decision = explain_tool_access(
        write_entry,
        active_mode=mode,
        phase_capabilities=(ToolCapability.READ, ToolCapability.SEARCH),
    )
    assert decision.available is False
    assert "active_mode_blocks_capability" in decision.reasons
    assert "phase_capability_mismatch" in decision.reasons


def test_snapshot_is_deterministic_bounded_and_json_safe() -> None:
    app = AppState.create()
    mcp = SimpleNamespace(
        all_tools=lambda: [inspect_document],
        status=lambda: {
            "healthy": {"status": "connected", "tool_count": 1, "required": False},
            "broken": {
                "status": "error",
                "tool_count": 0,
                "required": False,
                "url": "https://must-not-appear.example",
                "Authorization": "secret",
            },
        },
    )
    config = SimpleNamespace(
        app_state=app,
        all_plugin_tools=lambda: [write_document],
        mcp_registry=mcp,
        browser_manager=None,
        browser_tools=[],
        workspace_scope=None,
        workspace_access=SimpleNamespace(mode="safe"),
    )
    snapshot = build_authoring_snapshot(config, phase_name="design")
    encoded = json.dumps(snapshot.to_dict(), sort_keys=True)
    assert snapshot.catalog_version == AUTHORING_CATALOG_VERSION
    assert snapshot.snapshot_id
    assert "must-not-appear" not in encoded
    assert "Authorization" not in encoded
    assert [item["server"] for item in snapshot.mcp] == ["broken", "healthy"]
    assert snapshot.to_dict() == snapshot.to_dict()

    reference = snapshot.checkpoint_reference()
    assert set(reference) == {
        "catalog_version",
        "snapshot_id",
        "tool_fingerprints",
        "tool_names",
        "phase_name",
        "phase_role",
        "phase_capability_source",
        "phase_capabilities",
        "active_mode",
        "unavailable",
    }
    json.dumps(reference)


def test_browser_action_metadata_is_described_but_not_authoring_available() -> None:
    app = AppState.create()

    @tool()
    async def cloakbrowser_open_page(url: str) -> dict[str, object]:
        return {"url": url}

    config = SimpleNamespace(
        app_state=app,
        all_plugin_tools=lambda: [],
        mcp_registry=None,
        browser_tools=[cloakbrowser_open_page],
        browser_manager=SimpleNamespace(
            backend_name="cloakbrowser",
            enabled=True,
            settings=SimpleNamespace(allow_all_domains=False),
        ),
        workspace_scope=None,
        workspace_access=None,
    )
    snapshot = build_authoring_snapshot(config)
    entry = next(item for item in snapshot.tools if item.name == "cloakbrowser_open_page")
    assert entry.source == "browser"
    assert entry.optional_dependency == "cloakbrowser"
    assert entry.available is False
    assert entry.availability_reason == "authoring_excluded"


def test_snapshot_includes_live_global_introspection_and_role_provenance() -> None:
    app = AppState.create()
    config = SimpleNamespace(
        app_state=app,
        all_plugin_tools=lambda: [inspect_document],
        mcp_registry=None,
        browser_tools=[],
        browser_manager=None,
        workspace_scope=None,
        workspace_access=None,
    )
    snapshot = build_authoring_snapshot(
        config,
        phase_name="design",
        phase_role="planner",
    )
    names = {entry.name for entry in snapshot.tools}
    assert {
        "list_agenthicc_docs",
        "read_agenthicc_doc",
        "search_agenthicc_docs",
        "inspect_agenthicc_source",
        "search_agenthicc_source",
    } <= names
    assert snapshot.phase_role == "planner"
    assert snapshot.phase_capability_source == "role_default"
    assert set(snapshot.phase_capabilities) == {"read", "search", "git_read"}


def test_snapshot_cache_reuses_and_invalidates_on_mode_change() -> None:
    app = AppState.create()
    config = SimpleNamespace(
        app_state=app,
        all_plugin_tools=lambda: [inspect_document],
        mcp_registry=None,
        browser_tools=[],
        browser_manager=None,
        workspace_scope=None,
        workspace_access=None,
    )
    cache = AuthoringSnapshotCache(max_entries=2)
    first = cache.get_or_build(config, phase_name="design", tools=[inspect_document])
    assert cache.get_or_build(config, phase_name="design", tools=[inspect_document]) is first
    app.active_mode.set(
        SimpleNamespace(name="Plan", blocked_capabilities=frozenset({ToolCapability.WRITE}))
    )
    second = cache.get_or_build(
        config,
        phase_name="design",
        tools=[inspect_document],
    )
    assert second is not first
    cache.clear()
    assert cache.get_or_build(config, phase_name="design", tools=[inspect_document]) is not first


def test_snapshot_envelope_is_bounded_and_json_safe() -> None:
    huge = tuple(build_tool_catalog([inspect_document])[0] for _ in range(512))
    # Constructing entries with unique descriptions is unnecessary here; the
    # large session summaries exercise the final metadata truncation path.
    snapshot = AuthoringSnapshot(
        snapshot_id="snapshot",
        tools=huge,
        workspace={"root": "x" * 300_000},
    )
    encoded = json.dumps(snapshot.to_dict(), ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= 256_000
    assert snapshot.to_dict().get("tools_truncated") is True


@pytest.mark.asyncio
async def test_browser_inspection_returns_live_schemas_without_starting_browser() -> None:
    tools = make_inspection_tools()
    by_name = {getattr(item, "__name__", ""): item for item in tools}
    cloak = await by_name["describe_cloakbrowser_tools"]()
    playwright = await by_name["describe_playwright_tools"]()
    assert cloak["schema_version"] == "agenthicc.authoring-catalog.v1"
    assert playwright["schema_version"] == "agenthicc.authoring-catalog.v1"
    assert any(item["name"] == "cloakbrowser_open" for item in cloak["tools"])
    assert any(item["name"] == "playwright_open" for item in playwright["tools"])
    assert "operation_id" in cloak["constraints"]
