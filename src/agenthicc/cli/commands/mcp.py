"""CLI commands for persistent MCP server configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command, group, optional_positionals

if TYPE_CHECKING:
    from agenthicc.tools.mcp_manager import McpSessionManager


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


def _selected_path(ctx: CLIContext, global_: bool, project: bool) -> Path:
    from agenthicc.cli.mcp_config import mcp_config_path  # noqa: PLC0415

    return mcp_config_path(
        global_scope=global_,
        project_scope=project,
        project_dir=Path.cwd(),
        user_dir=Path.home(),
        explicit_path=ctx.config_path,
    )


def _redacted_servers(path: Path) -> list[dict[str, object]]:
    from agenthicc.cli.mcp_config import read_mcp_servers  # noqa: PLC0415
    from agenthicc.tools.mcp import McpServerConfig  # noqa: PLC0415

    result: list[dict[str, object]] = []
    for raw in read_mcp_servers(path):
        try:
            result.append(McpServerConfig.from_dict(raw).redacted())
        except Exception:  # noqa: BLE001
            result.append({"name": raw.get("name", "<invalid>"), "status": "invalid"})
    return result


@command("mcp", "list", help="List configured MCP servers")
def mcp_list(
    ctx: CLIContext,
    global_: bool = False,
    project: bool = False,
    json_: bool = False,
) -> None:
    """List configuration without starting any MCP server."""
    from agenthicc.cli.mcp_config import McpConfigError  # noqa: PLC0415

    path = _selected_path(ctx, global_, project)
    try:
        result = _redacted_servers(path)
    except McpConfigError as exc:
        print(f"error: {exc}")
        return
    if json_:
        print(json.dumps({"path": str(path), "servers": result}, sort_keys=True))
        return
    if not result:
        print(f"No MCP servers configured in {path}")
        return
    for item in result:
        print(
            f"{item.get('name', '<invalid>')}: "
            f"transport={item.get('transport', 'unknown')} "
            f"enabled={item.get('enabled', True)}"
        )


@command("mcp", "get", help="Show one configured MCP server")
def mcp_get(
    ctx: CLIContext,
    name: str,
    global_: bool = False,
    project: bool = False,
    json_: bool = False,
) -> None:
    path = _selected_path(ctx, global_, project)
    result = next((item for item in _redacted_servers(path) if item.get("name") == name), None)
    if result is None:
        print(f"error: MCP server not found: {name}")
        return
    print(json.dumps(result, sort_keys=True) if json_ else json.dumps(result, indent=2, sort_keys=True))


@command("mcp", "remove", help="Remove one configured MCP server")
def mcp_remove(
    ctx: CLIContext,
    name: str,
    global_: bool = False,
    project: bool = False,
) -> None:
    from agenthicc.cli.mcp_config import McpConfigError, remove_mcp_server  # noqa: PLC0415

    try:
        result = remove_mcp_server(
            name=name,
            global_scope=global_,
            project_scope=project,
            project_dir=Path.cwd(),
            user_dir=Path.home(),
            explicit_path=ctx.config_path,
        )
    except McpConfigError as exc:
        print(f"error: {exc}")
        return
    print(f"Removed MCP server {result.name!r} from {result.path}")


async def _live_cli_manager(ctx: CLIContext) -> "McpSessionManager":
    """Build a short-lived manager for explicit CLI diagnostics/actions."""
    from agenthicc.cli.mcp_config import mcp_config_path, read_mcp_servers  # noqa: PLC0415
    from agenthicc.config import load_config  # noqa: PLC0415
    from agenthicc.tools.mcp import McpServerConfig  # noqa: PLC0415
    from agenthicc.tools.mcp_manager import McpSessionManager  # noqa: PLC0415
    from agenthicc.tools.sandbox import NetworkGuard  # noqa: PLC0415

    path = mcp_config_path(
        project_dir=Path.cwd(),
        user_dir=Path.home(),
        explicit_path=ctx.config_path,
    )
    raw = read_mcp_servers(path)
    try:
        loaded_config = load_config(project_path=path, env_overrides=False)
        network_domains = loaded_config.security.network_allow_list
    except Exception:  # noqa: BLE001 - MCP diagnostics still report server failures
        network_domains = []
    manager = McpSessionManager(
        [McpServerConfig.from_dict(item) for item in raw],
        workspace_root=Path.cwd(),
        network_guard=NetworkGuard(network_domains) if network_domains else None,
    )
    return manager


@command("mcp", "connect", help="Connect to one MCP server and inspect its catalog")
async def mcp_connect(ctx: CLIContext, name: str) -> None:
    manager = None
    try:
        manager = await _live_cli_manager(ctx)
        await manager.connect_server(name)
        print(json.dumps(manager.status(name), sort_keys=True))
    except Exception as exc:  # noqa: BLE001
        print(f"error: MCP connect failed for {name!r}: {type(exc).__name__}: {exc}")
    finally:
        if manager is not None:
            await manager.shutdown()


@command("mcp", "disconnect", help="Disconnect an MCP server in the current process")
def mcp_disconnect(ctx: CLIContext, name: str) -> None:
    print(
        f"MCP server {name!r} is session-scoped; run /mcp disconnect {name} "
        "inside the active agenthicc session."
    )


@command("mcp", "refresh", help="Refresh one MCP server's tool catalog")
async def mcp_refresh(ctx: CLIContext, name: str) -> None:
    manager = None
    try:
        manager = await _live_cli_manager(ctx)
        snapshot = await manager.refresh_server(name)
        print(json.dumps(snapshot.to_dict() if snapshot else {"status": "unavailable"}, sort_keys=True))
    except Exception as exc:  # noqa: BLE001
        print(f"error: MCP refresh failed for {name!r}: {type(exc).__name__}: {exc}")
    finally:
        if manager is not None:
            await manager.shutdown()


@optional_positionals("name")
@command("mcp", "doctor", help="Validate and diagnose MCP server connectivity")
async def mcp_doctor(ctx: CLIContext, name: str = "", json_: bool = False) -> None:
    manager = None
    result: list[dict[str, object]] | dict[str, object]
    try:
        manager = await _live_cli_manager(ctx)
        result = await manager.doctor(name or None)
        print(json.dumps(result, sort_keys=True) if json_ else json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:  # noqa: BLE001
        result = {"status": "invalid", "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, sort_keys=True) if json_ else json.dumps(result, indent=2, sort_keys=True))
    finally:
        if manager is not None:
            await manager.shutdown()


@command("mcp", "auth", help="Check configured MCP authentication")
def mcp_auth(ctx: CLIContext, name: str) -> None:
    from agenthicc.cli.mcp_config import mcp_config_path, read_mcp_servers  # noqa: PLC0415

    path = mcp_config_path(
        project_dir=Path.cwd(),
        user_dir=Path.home(),
        explicit_path=ctx.config_path,
    )
    try:
        item = next(item for item in read_mcp_servers(path) if item.get("name") == name)
    except Exception as exc:  # noqa: BLE001
        print(f"error: MCP server not found or invalid: {name} ({exc})")
        return
    env_headers = item.get("env_headers", {})
    token = item.get("token", "")
    references = len(env_headers) if isinstance(env_headers, dict) else 0
    configured = bool(references or (isinstance(token, str) and token.startswith("${")))
    print(json.dumps({"server": name, "status": "configured" if configured else "not_configured"}))


@command("mcp", "logout", help="Remove stored MCP authentication")
def mcp_logout(ctx: CLIContext, name: str) -> None:
    # Environment-backed credentials are intentionally not mutated by the CLI.
    print(f"No persisted MCP credential was removed for {name!r}; environment-backed credentials remain unchanged.")
