"""Unit tests for the plugin system (PRD-13)."""
from __future__ import annotations
import pytest
from unittest.mock import MagicMock, patch
from agenthicc.plugin import AgenthiccPlugin, PluginLoadError, PluginRegistry
from agenthicc.tools.base import Tool

pytestmark = pytest.mark.unit


class _HelloTool(Tool):
    name = "hello"; description = "Say hello."; parameters = {}
    async def execute(self, args, context): return "hi"


class _SimplePlugin(AgenthiccPlugin):
    @property
    def name(self): return "test-plugin"
    def on_load(self, registry, config=None): registry.register_tool(_HelloTool())


def _mock_entry_point(plugin_cls):
    ep = MagicMock(); ep.name = plugin_cls().name; ep.load.return_value = plugin_cls
    return ep


class TestPluginRegistry:
    def test_register_tool(self):
        reg = PluginRegistry()
        reg.register_tool(_HelloTool())
        assert any(t.name == "hello" for t in reg.tools)

    def test_register_agent_type(self):
        reg = PluginRegistry()
        reg.register_agent_type("MyBot", object)
        assert "MyBot" in reg.agent_types

    def test_register_event_handler(self):
        reg = PluginRegistry()
        reg.register_event_handler("MyEvent", lambda s, e: (s, []))
        assert "MyEvent" in reg.event_handlers

    def test_register_command_calls_session(self):
        from agenthicc.tui.input.completions import CommandSpec
        mock_session = MagicMock()
        reg = PluginRegistry(input_bar_session=mock_session)
        spec = CommandSpec("/test", "Test")
        reg.register_command(spec)
        mock_session.register_command.assert_called_once_with(spec)

    def test_register_hook_calls_hook_runner(self):
        mock_runner = MagicMock()
        reg = PluginRegistry(hook_runner=mock_runner)
        hook = MagicMock()
        reg.register_hook("tool", "before", hook)
        mock_runner.registry.register.assert_called_once_with("tool", "before", hook)

    def test_load_calls_on_load(self):
        reg = PluginRegistry()
        with patch("importlib.metadata.entry_points", return_value=[_mock_entry_point(_SimplePlugin)]):
            reg.discover()
            reg.load("test-plugin")
        assert any(t.name == "hello" for t in reg.tools)

    def test_load_sets_manifest_loaded(self):
        reg = PluginRegistry()
        with patch("importlib.metadata.entry_points", return_value=[_mock_entry_point(_SimplePlugin)]):
            reg.discover()
            manifest = reg.load("test-plugin")
        assert manifest.status == "loaded"

    def test_broken_plugin_sets_error_status(self):
        class _Broken(AgenthiccPlugin):
            @property
            def name(self): return "broken"
            def on_load(self, registry, config=None): raise RuntimeError("oops")
        reg = PluginRegistry()
        with patch("importlib.metadata.entry_points", return_value=[_mock_entry_point(_Broken)]):
            reg.discover()
            manifest = reg.load("broken")
        assert manifest.status == "error"
        assert "oops" in manifest.error

    def test_discover_empty_returns_empty(self):
        reg = PluginRegistry()
        with patch("importlib.metadata.entry_points", return_value=[]):
            manifests = reg.discover()
        assert manifests == []

    def test_load_unknown_raises(self):
        reg = PluginRegistry()
        with patch("importlib.metadata.entry_points", return_value=[]):
            reg.discover()
        with pytest.raises(PluginLoadError):
            reg.load("does-not-exist")

    def test_reload_calls_on_unload(self):
        unload_called = []
        class _Plugin(AgenthiccPlugin):
            @property
            def name(self): return "rel"
            def on_load(self, r, c=None): pass
            def on_unload(self): unload_called.append(True)
        reg = PluginRegistry()
        with patch("importlib.metadata.entry_points", return_value=[_mock_entry_point(_Plugin)]):
            reg.discover(); reg.load("rel"); reg.reload("rel")
        assert unload_called

    def test_manifests_property(self):
        reg = PluginRegistry()
        with patch("importlib.metadata.entry_points", return_value=[_mock_entry_point(_SimplePlugin)]):
            reg.discover()
        assert len(reg.manifests) == 1

    def test_load_all(self):
        reg = PluginRegistry()
        with patch("importlib.metadata.entry_points", return_value=[_mock_entry_point(_SimplePlugin)]):
            reg.load_all(["test-plugin"])
        assert any(t.name == "hello" for t in reg.tools)
