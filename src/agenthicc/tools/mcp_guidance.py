"""Agent-facing guidance for configuring and connecting MCP servers.

This tool is deliberately read-only.  It gives an agent the authoritative
configuration shape and the exact file/CLI locations, while leaving the
decision to edit a project configuration and the act of connecting to the
user or to ordinary filesystem/terminal tools.

NOTE: no ``from __future__ import annotations`` — ``@tool()`` inspects real
annotations when it builds the provider schema.
"""

from lauren_ai._tools import tool

from agenthicc.tools.capabilities import tool_exploratory, tool_read

__all__ = ["mcp_connection_guide", "MCP_AGENT_TOOLS"]


@tool_exploratory
@tool_read
@tool(name="mcp_connection_guide")
async def mcp_connection_guide(
    transport: str = "streamable_http",
    scope: str = "project",
    server_name: str = "my-server",
) -> dict[str, object]:
    """Explain how to configure and connect an MCP server in AgentHICC.

    Use this before editing configuration or asking the user to install an MCP
    server. It returns the exact recommended TOML path, a safe CLI command,
    transport-specific configuration, credential handling, and the commands
    for connecting and reloading the session-owned MCP manager. It never reads
    or writes configuration and never handles secret values.

    Args:
        transport: ``stdio`` for a local process, or ``streamable_http`` for a
            remote HTTP endpoint. ``sse`` and ``ws`` are also supported.
        scope: ``project`` writes the current project's config; ``global``
            writes the user config shared by projects.
        server_name: Name to use in the example. It must not contain spaces or
            a colon when copied into a real config.
    """
    requested_transport = transport.strip().lower()
    aliases = {
        "streamable": "streamable_http",
        "http": "streamable_http",
        "websocket": "ws",
    }
    normalized_transport = aliases.get(requested_transport, requested_transport)
    if normalized_transport not in {"stdio", "streamable_http", "sse", "ws"}:
        return {
            "ok": False,
            "error": f"Unsupported MCP transport: {transport!r}.",
            "supported_transports": ["stdio", "streamable_http", "sse", "ws"],
        }

    normalized_scope = scope.strip().lower()
    if normalized_scope not in {"project", "global"}:
        return {
            "ok": False,
            "error": f"Unsupported config scope: {scope!r}.",
            "supported_scopes": ["project", "global"],
        }

    clean_name = server_name.strip() or "my-server"
    if ":" in clean_name or any(character.isspace() for character in clean_name):
        return {
            "ok": False,
            "error": "server_name must not contain spaces or ':' when copied into config.",
        }

    config_path = (
        ".agenthicc/agenthicc.toml"
        if normalized_scope == "project"
        else "~/.agenthicc/agenthicc.toml"
    )
    target_flag = "--project" if normalized_scope == "project" else "--global"

    if normalized_transport == "stdio":
        example_url = "python -m my_mcp_server"
        cli_command = f"agenthicc mcp add {clean_name} '{example_url}' {target_flag}"
        toml = (
            "[[tools.mcp_servers]]\n"
            f'name = "{clean_name}"\n'
            'transport = "stdio"\n'
            f'url = "{example_url}"\n'
            "auto_connect = true\n"
        )
        setup = [
            "Install the MCP server in the same environment that runs agenthicc.",
            f"Run `{cli_command}` or write the TOML block to `{config_path}`.",
            "For a Lauren MCP directory/server.py, pass that path to `mcp add`; it is converted to an lmcp stdio launcher.",
        ]
    else:
        example_url = "https://mcp.example.test/mcp"
        cli_transport = (
            "streamable" if normalized_transport == "streamable_http" else normalized_transport
        )
        cli_command = (
            f"agenthicc mcp add {clean_name} '{example_url}' "
            f"--transport {cli_transport} {target_flag}"
        )
        toml = (
            "[[tools.mcp_servers]]\n"
            f'name = "{clean_name}"\n'
            f'transport = "{normalized_transport}"\n'
            f'url = "{example_url}"\n'
            "auto_connect = true\n"
            "startup_timeout_s = 10\n"
            "tool_timeout_s = 60\n\n"
            "[tools.mcp_servers.env_headers]\n"
            'Authorization = "MCP_TOKEN"\n'
        )
        setup = [
            "Export the token in the shell before launching agenthicc: `export MCP_TOKEN='…'`.",
            f"Run `{cli_command} --token-env MCP_TOKEN` or write the TOML block to `{config_path}`.",
            "Never put a literal bearer token in TOML, tool arguments, source files, or prompts.",
        ]

    return {
        "ok": True,
        "purpose": "Configure one MCP server and make its advertised tools available to the active AgentHICC session.",
        "config_path": config_path,
        "scope": normalized_scope,
        "transport": normalized_transport,
        "setup": setup,
        "cli_command": cli_command,
        "toml": toml,
        "session_commands": [
            "/mcp status — inspect redacted status and tool counts",
            "/mcp connect NAME — connect one server explicitly",
            "/mcp refresh NAME — refresh one server's catalog",
            "/mcp reload — disconnect and reconnect every enabled configured server",
            "/mcp doctor [NAME] — validate and diagnose connectivity",
        ],
        "important": [
            "Configuration is loaded at session startup; after editing it, restart the session or use the session's /mcp reload when the server is already registered.",
            "MCP tools are session-scoped and are shared by chat, Plan mode, workflows, and subagents.",
            "An optional server can fail without blocking healthy servers; set required = true only when startup must fail closed.",
            "Use one env_headers mapping per server when different servers need different environment variables.",
        ],
        "documentation": "docs/guides/mcp.md",
    }


MCP_AGENT_TOOLS = [mcp_connection_guide]
