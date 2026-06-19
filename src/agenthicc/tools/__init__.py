"""Agenthicc tool layer — sandbox, base types, and MCP bridge."""

from .base import Tool, ToolResultEnvelope
from .sandbox import NetworkGuard, WorkspaceView

__all__ = [
    "NetworkGuard",
    "Tool",
    "ToolResultEnvelope",
    "WorkspaceView",
]

# MCP bridge and registry (PRD-28) — optional; requires lauren_mcp
try:
    from .mcp import (  # noqa: F401
        AgenthiccMcpTool,
        McpServerConfig,
        McpToolBridge,
        McpToolCallError,
        McpToolRegistry,
        McpToolSchema,
    )

    __all__ += [
        "AgenthiccMcpTool",
        "McpServerConfig",
        "McpToolBridge",
        "McpToolCallError",
        "McpToolRegistry",
        "McpToolSchema",
    ]
except ImportError:
    pass
