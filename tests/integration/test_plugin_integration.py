"""Integration tests for the plugin system with a live kernel (PRD-13)."""
from __future__ import annotations
import asyncio
import pytest
from unittest.mock import patch, MagicMock
from agenthicc.plugin import AgenthiccPlugin, PluginRegistry
from agenthicc.tools.base import Tool

pytestmark = pytest.mark.integration


class _SimpleTool(Tool):
    name = "integration_tool"; description = "Test."; parameters = {}
    async def execute(self, args, context): return "ok"


class _ContribPlugin(AgenthiccPlugin):
    @property
    def name(self): return "contrib"
    def on_load(self, registry, config=None):
        registry.register_tool(_SimpleTool())


async def test_register_tool_emits_kernel_event(running_processor):
    # PluginRegistry.register_tool emits via asyncio.ensure_future;
    # wait a short time for the event to be processed.
    import asyncio
    reg = PluginRegistry(event_processor=running_processor)
    reg.register_tool(_SimpleTool())
    await asyncio.sleep(0.1)
    await running_processor.drain()
    types = [e.event_type for e in running_processor.event_log]
    assert "ToolRegistered" in types


async def test_load_plugin_via_mocked_entry_point(running_processor):
    reg = PluginRegistry(event_processor=running_processor)
    ep = MagicMock(); ep.name = "contrib"; ep.load.return_value = _ContribPlugin
    with patch("importlib.metadata.entry_points", return_value=[ep]):
        reg.discover()
        manifest = reg.load("contrib")
    import asyncio
    await asyncio.sleep(0.1)
    await running_processor.drain()
    assert manifest.status == "loaded"
    assert any(t.name == "integration_tool" for t in reg.tools)
    assert any(e.event_type == "ToolRegistered" for e in running_processor.event_log)


async def test_load_all_registers_multiple_tools(running_processor):
    class _T2(Tool):
        name = "tool2"; description = ""; parameters = {}
        async def execute(self, a, c): return "t2"
    class _P2(AgenthiccPlugin):
        @property
        def name(self): return "p2"
        def on_load(self, r, c=None): r.register_tool(_T2())
    reg = PluginRegistry(event_processor=running_processor)
    ep1 = MagicMock(); ep1.name = "contrib"; ep1.load.return_value = _ContribPlugin
    ep2 = MagicMock(); ep2.name = "p2"; ep2.load.return_value = _P2
    with patch("importlib.metadata.entry_points", return_value=[ep1, ep2]):
        reg.load_all(["contrib", "p2"])
    names = {t.name for t in reg.tools}
    assert "integration_tool" in names and "tool2" in names
