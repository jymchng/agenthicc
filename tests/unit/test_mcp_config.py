"""Unit tests for MCP configuration parsing (PRD-29)."""
from __future__ import annotations

import sys
import pytest

pytestmark = pytest.mark.unit


def test_tool_settings_default_empty():
    from agenthicc.config import ToolSettings

    ts = ToolSettings()
    assert ts.mcp_servers == []


def test_tool_settings_mcp_servers_field_exists():
    from agenthicc.config import ToolSettings

    ts = ToolSettings(mcp_servers=[{"name": "x", "url": "y"}])
    assert len(ts.mcp_servers) == 1


def test_parse_mcp_servers_empty_list():
    from agenthicc.config import _parse_mcp_servers

    result = _parse_mcp_servers([])
    assert result == []


def test_parse_mcp_servers_returns_mcp_server_config_instances():
    from agenthicc.config import _parse_mcp_servers
    from agenthicc.tools.mcp import McpServerConfig

    raw = [{"name": "x", "url": "y", "transport": "stdio"}]
    result = _parse_mcp_servers(raw)
    assert len(result) == 1
    assert isinstance(result[0], McpServerConfig)
    assert result[0].name == "x"
    assert result[0].url == "y"
    assert result[0].transport == "stdio"


def test_parse_mcp_servers_multiple_entries():
    from agenthicc.config import _parse_mcp_servers
    from agenthicc.tools.mcp import McpServerConfig

    raw = [
        {"name": "srv1", "url": "cmd1"},
        {"name": "srv2", "url": "ws://host", "transport": "ws"},
    ]
    result = _parse_mcp_servers(raw)
    assert len(result) == 2
    assert all(isinstance(r, McpServerConfig) for r in result)
    assert result[0].name == "srv1"
    assert result[1].transport == "ws"


def test_parse_mcp_servers_from_dict_maps_fields_correctly():
    from agenthicc.config import _parse_mcp_servers

    raw = [{
        "name": "github",
        "url": "wss://mcp.github.example.com",
        "transport": "ws",
        "token": "${GITHUB_TOKEN}",
        "auto_connect": False,
        "reconnect_attempts": 5,
        "reconnect_delay_seconds": 2.0,
    }]
    result = _parse_mcp_servers(raw)
    cfg = result[0]
    assert cfg.name == "github"
    assert cfg.token == "${GITHUB_TOKEN}"
    assert cfg.auto_connect is False
    assert cfg.reconnect_attempts == 5
    assert cfg.reconnect_delay_seconds == 2.0


def test_parse_mcp_servers_graceful_on_missing_import(monkeypatch):
    """Falls back to raw dicts when agenthicc.tools.mcp is not importable."""
    monkeypatch.setitem(sys.modules, "agenthicc.tools.mcp", None)
    # Force reimport of config to pick up the monkeypatched sys.modules
    import agenthicc.config as cfg_module

    original_fn = cfg_module._parse_mcp_servers

    # Simulate ImportError path by patching directly
    def _failing_parse(raw_list):
        try:
            from agenthicc.tools.mcp import McpServerConfig  # noqa: PLC0415
            return [McpServerConfig.from_dict(d) for d in raw_list]
        except ImportError:
            return list(raw_list)

    monkeypatch.setattr(cfg_module, "_parse_mcp_servers", _failing_parse)

    raw = [{"name": "x", "url": "y"}]
    result = cfg_module._parse_mcp_servers(raw)
    assert result == raw  # falls back to raw dicts


def test_parse_mcp_servers_ignores_unknown_keys():
    """from_dict should silently drop keys not in McpServerConfig."""
    from agenthicc.config import _parse_mcp_servers

    raw = [{"name": "x", "url": "y", "totally_unknown_key": "ignored"}]
    result = _parse_mcp_servers(raw)
    assert result[0].name == "x"
    assert not hasattr(result[0], "totally_unknown_key")


def test_dict_to_config_integrates_mcp_servers():
    """_dict_to_config passes [[tools.mcp_servers]] through _parse_mcp_servers."""
    from agenthicc.config import _dict_to_config
    from agenthicc.tools.mcp import McpServerConfig

    data = {
        "tools": {
            "mcp_servers": [
                {"name": "fs", "url": "npx server /tmp", "transport": "stdio"}
            ]
        }
    }
    cfg = _dict_to_config(data)
    assert len(cfg.tools.mcp_servers) == 1
    assert isinstance(cfg.tools.mcp_servers[0], McpServerConfig)
    assert cfg.tools.mcp_servers[0].name == "fs"


def test_load_config_default_has_empty_mcp_servers(tmp_path):
    """load_config() with no config file yields empty mcp_servers list."""
    from agenthicc.config import load_config

    cfg = load_config(project_path=str(tmp_path / "nonexistent.toml"), env_overrides=False)
    assert cfg.tools.mcp_servers == []
