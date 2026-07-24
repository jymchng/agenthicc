"""Coverage for persistent ``agenthicc mcp add`` configuration."""

from __future__ import annotations

import argparse
import stat
import tomllib
from pathlib import Path

import pytest

from agenthicc.cli.context import CLIContext
from agenthicc.cli.mcp_config import McpConfigError, add_mcp_server

pytestmark = pytest.mark.unit


def test_add_project_server_writes_canonical_config_and_loads(tmp_path: Path) -> None:
    project = tmp_path / "project"

    result = add_mcp_server(
        name="local-tools",
        url="python -m my_mcp_server",
        project_dir=project,
    )

    assert result.scope == "project"
    assert result.path == project / ".agenthicc" / "agenthicc.toml"
    data = tomllib.loads(result.path.read_text(encoding="utf-8"))
    assert data["tools"]["mcp_servers"] == [
        {
            "name": "local-tools",
            "url": "python -m my_mcp_server",
            "transport": "stdio",
        }
    ]

    from agenthicc.config import load_config

    config = load_config(
        project_path=result.path,
        user_path=tmp_path / "missing-user.toml",
        env_overrides=False,
    )
    server = config.tools.mcp_servers[0]
    assert server.name == "local-tools"
    assert server.url == "python -m my_mcp_server"


def test_add_global_server_uses_global_config_root(tmp_path: Path) -> None:
    user = tmp_path / "user"

    result = add_mcp_server(
        name="remote",
        url="https://mcp.example.test/server",
        transport="streamable",
        global_scope=True,
        user_dir=user,
    )

    assert result.scope == "global"
    assert result.path == user / ".agenthicc" / "agenthicc.toml"
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    assert 'transport = "streamable"' in result.path.read_text(encoding="utf-8")


def test_add_preserves_config_and_stores_token_reference(tmp_path: Path) -> None:
    config_path = tmp_path / ".agenthicc" / "agenthicc.toml"
    config_path.parent.mkdir()
    config_path.write_text('[execution]\nmodel = "test-model"\n', encoding="utf-8")
    config_path.chmod(0o640)

    result = add_mcp_server(
        name="secured",
        url="https://mcp.example.test/server",
        transport="http",
        token_env="MCP_TOKEN",
        auto_connect=False,
        reconnect_attempts=5,
        reconnect_delay_seconds=2.5,
        explicit_path=str(config_path),
    )

    assert stat.S_IMODE(result.path.stat().st_mode) == 0o640
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["execution"]["model"] == "test-model"
    assert data["tools"]["mcp_servers"][0] == {
        "name": "secured",
        "url": "https://mcp.example.test/server",
        "transport": "http",
        "token": "${MCP_TOKEN}",
        "auto_connect": False,
        "reconnect_attempts": 5,
        "reconnect_delay_seconds": 2.5,
    }


def test_invalid_or_duplicate_add_never_changes_existing_file(tmp_path: Path) -> None:
    config_path = tmp_path / "agenthicc.toml"
    config_path.write_text(
        '[[tools.mcp_servers]]\nname = "existing"\nurl = "command"\n',
        encoding="utf-8",
    )
    original = config_path.read_text(encoding="utf-8")

    with pytest.raises(McpConfigError, match="already exists"):
        add_mcp_server(name="existing", url="other", explicit_path=str(config_path))
    assert config_path.read_text(encoding="utf-8") == original

    with pytest.raises(McpConfigError, match="unsupported transport"):
        add_mcp_server(
            name="new",
            url="command",
            transport="unknown",
            explicit_path=str(config_path),
        )
    assert config_path.read_text(encoding="utf-8") == original


def test_malformed_config_is_rejected_without_repairing_it(tmp_path: Path) -> None:
    config_path = tmp_path / "agenthicc.toml"
    config_path.write_text("[tools\n", encoding="utf-8")

    with pytest.raises(McpConfigError, match="cannot safely update"):
        add_mcp_server(name="new", url="command", explicit_path=str(config_path))
    assert config_path.read_text(encoding="utf-8") == "[tools\n"


def test_scope_and_secret_validation() -> None:
    with pytest.raises(McpConfigError, match="only one target"):
        add_mcp_server(name="x", url="command", global_scope=True, project_scope=True)
    with pytest.raises(McpConfigError, match="uppercase environment"):
        add_mcp_server(name="x", url="command", token_env="token")
    with pytest.raises(McpConfigError, match="cannot be negative"):
        add_mcp_server(name="x", url="command", reconnect_attempts=-1)


def test_mcp_add_handler_reports_configuration_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from agenthicc.cli.commands import mcp

    def fail(**kwargs: object) -> object:
        raise McpConfigError("bad MCP configuration")

    monkeypatch.setattr("agenthicc.cli.mcp_config.add_mcp_server", fail)
    mcp.mcp_add(CLIContext(), "name", "url")
    assert "error: bad MCP configuration" in capsys.readouterr().out


def test_mcp_add_parser_wires_positional_and_options() -> None:
    from agenthicc.cli import registry
    from agenthicc.cli.commands import mcp

    parser = argparse.ArgumentParser()
    registry._wire(parser, registry._as_tree())
    namespace = parser.parse_args(
        [
            "mcp",
            "add",
            "remote",
            "https://mcp.example.test",
            "--global",
            "--transport",
            "http",
            "--token-env",
            "MCP_TOKEN",
            "--no-auto-connect",
        ]
    )

    assert namespace.name == "remote"
    assert namespace.url == "https://mcp.example.test"
    assert namespace.global_ is True
    assert namespace.transport == "http"
    assert namespace.token_env == "MCP_TOKEN"
    assert namespace.no_auto_connect is True
    assert mcp.mcp_add is not None
