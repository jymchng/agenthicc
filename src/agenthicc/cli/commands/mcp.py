"""CLI commands for persistent MCP server configuration."""

from __future__ import annotations

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command, group


@group("mcp", help="Manage configured MCP servers")
def _mcp_group() -> None: ...


@command("mcp", "add", help="Add an MCP server to configuration")
def mcp_add(
    ctx: CLIContext,
    name: str,
    url: str,
    global_: bool = False,
    project: bool = False,
    transport: str = "stdio",
    token_env: str = "",
    no_auto_connect: bool = False,
    reconnect_attempts: int = 3,
    reconnect_delay_seconds: float = 1.0,
) -> None:
    """Persist NAME and URL as one ``[[tools.mcp_servers]]`` entry."""
    from agenthicc.cli.mcp_config import McpConfigError, add_mcp_server  # noqa: PLC0415

    try:
        result = add_mcp_server(
            name=name,
            url=url,
            transport=transport,
            token_env=token_env,
            auto_connect=not no_auto_connect,
            reconnect_attempts=reconnect_attempts,
            reconnect_delay_seconds=reconnect_delay_seconds,
            global_scope=global_,
            project_scope=project,
            explicit_path=ctx.config_path,
        )
    except McpConfigError as exc:
        print(f"error: {exc}")
        return

    print(f"Added MCP server {result.name!r} ({result.scope}) to {result.path}")
