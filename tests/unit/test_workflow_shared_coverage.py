"""Coverage for shared workflow infrastructure and memory-tool fallbacks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.memory_tools import make_memory_tools

pytestmark = pytest.mark.unit


def _by_name(tools: list[object]) -> dict[str, object]:
    return {getattr(item, "__name__", ""): item for item in tools}


@pytest.mark.asyncio
async def test_memory_tools_fail_closed_when_optional_services_are_absent() -> None:
    tools = _by_name(make_memory_tools(None, None))
    assert (await tools["memory_write"]("key", "value"))["error"] == "memory_not_available"  # type: ignore[operator]
    assert (await tools["memory_read"]("key"))["found"] is False  # type: ignore[operator]
    assert (await tools["semantic_search"]("query"))["error"] == "semantic_index_not_available"  # type: ignore[operator]
    assert (await tools["publish_artifact"]("content"))["artifact_id"] is None  # type: ignore[operator]


@pytest.mark.asyncio
async def test_memory_tools_forward_scopes_ttls_and_search_hits() -> None:
    calls: list[tuple[object, ...]] = []

    class Router:
        async def write(self, *args: object, **kwargs: object) -> dict[str, object]:
            calls.append(("write", *args, *kwargs.values()))
            return {"ok": True}

        async def read(self, *args: object, **kwargs: object) -> dict[str, object]:
            calls.append(("read", *args, *kwargs.values()))
            return {"found": True, "value": "stored"}

        async def publish_artifact(self, *args: object, **kwargs: object) -> dict[str, object]:
            calls.append(("publish", *args, *kwargs.values()))
            return {"ok": True, "artifact_id": "a1"}

    class Index:
        async def search(self, query: str, *, top_k: int) -> list[tuple[str, float]]:
            calls.append(("search", query, top_k))
            return [("doc", 0.123456)]

    tools = _by_name(make_memory_tools(Router(), Index()))
    assert (await tools["memory_write"]("k", "v", ttl_seconds=4.0))["ok"] is True  # type: ignore[operator]
    assert (await tools["memory_write"]("k", "v", ttl_seconds=0.0))["ok"] is True  # type: ignore[operator]
    assert (await tools["memory_read"]("k", scope="global", namespace="notes"))["found"] is True  # type: ignore[operator]
    assert (await tools["semantic_search"]("query", top_k=2))["results"] == [  # type: ignore[operator]
        {"doc_id": "doc", "score": 0.1235}
    ]
    assert (await tools["publish_artifact"]("content", content_type="text/markdown"))[
        "artifact_id"
    ] == "a1"  # type: ignore[operator]
    assert any(call[0] == "write" for call in calls)
    assert any(call[0] == "search" for call in calls)


@pytest.mark.asyncio
async def test_workflow_config_materializes_tools_and_awaits_startup() -> None:
    startup_calls: list[tuple[str, ...]] = []

    class Startup:
        async def wait_for(self, *phases: str) -> None:
            startup_calls.append(phases)

    plugin = SimpleNamespace(all_tools=["plugin"])
    config = WorkflowConfig(
        conv_store=SimpleNamespace(),
        app_state=SimpleNamespace(),
        processor=SimpleNamespace(),
        agent_runner=SimpleNamespace(),
        approval_svc=None,
        cfg=SimpleNamespace(),
        skills={},
        plugin_tools=plugin,  # type: ignore[arg-type]
        mcp_registry=None,
        mention_cache=SimpleNamespace(),
        agents_registry=SimpleNamespace(),
        browser_tools=("browser",),
        startup=Startup(),  # type: ignore[arg-type]
        required_startup_phases=("mcp",),
    )
    assert config.all_plugin_tools() == ["plugin", "browser"]
    await config.wait_for_startup()
    await config.wait_for_startup("extensions")
    assert startup_calls == [("mcp",), ("extensions",)]

    no_startup = WorkflowConfig(
        conv_store=SimpleNamespace(),
        app_state=SimpleNamespace(),
        processor=SimpleNamespace(),
        agent_runner=SimpleNamespace(),
        approval_svc=None,
        cfg=SimpleNamespace(),
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=SimpleNamespace(),
        agents_registry=SimpleNamespace(),
    )
    await no_startup.wait_for_startup()
    with pytest.raises(RuntimeError, match="readiness is unavailable"):
        await no_startup.wait_for_startup("mcp")
