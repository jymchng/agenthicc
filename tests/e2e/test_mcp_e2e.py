"""E2E MCP tests — skipped unless AGENTHICC_MCP_E2E=1 and npx is available."""
import os
import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("AGENTHICC_MCP_E2E"),
        reason="Set AGENTHICC_MCP_E2E=1 to run (requires npx and npm packages)",
    ),
]

@pytest.mark.asyncio
async def test_filesystem_server_real_connection():
    """Connect to reference MCP filesystem server and list tools."""
    from agenthicc.tools.mcp import McpServerConfig, McpToolRegistry
    cfg = McpServerConfig(
        name="fs",
        url="npx -y @modelcontextprotocol/server-filesystem /tmp",
        transport="stdio",
        auto_connect=True,
    )
    reg = McpToolRegistry()
    reg.register_server(cfg)
    tools = await reg.discover_all()
    await reg.shutdown()
    assert len(tools) > 0, "Should discover tools from filesystem server"
    names = {t.name for t in tools}
    assert any("list" in n or "read" in n for n in names)
