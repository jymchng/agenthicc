"""Agenthicc tool layer — sandbox, base types, and MCP bridge."""

from .base import Tool, ToolBase, ToolResult, ToolResultEnvelope
from .context import ToolCallContext
from .executor import (
    AgenthiccToolExecutor,
    ApprovalDecision,
    ToolErrorKind,
    ToolExecutor,
    ToolMetadata,
)
from .hooks import (
    AfterToolHookDecision,
    BeforeToolHookDecision,
    ErrorToolHookDecision,
    HookRegistry,
    HookRunner,
    LifecycleHook,
    ToolHook,
)
from .sandbox import NetworkGuard, ResourceLimits, ToolSandbox, WorkspaceView

__all__ = [
    "NetworkGuard",
    "ResourceLimits",
    "Tool",
    "ToolBase",
    "ToolResult",
    "ToolResultEnvelope",
    "WorkspaceView",
    "AgenthiccToolExecutor",
    "ApprovalDecision",
    "ToolCallContext",
    "ToolErrorKind",
    "ToolExecutor",
    "ToolMetadata",
    "ToolSandbox",
    "AfterToolHookDecision",
    "BeforeToolHookDecision",
    "ErrorToolHookDecision",
    "HookRegistry",
    "HookRunner",
    "LifecycleHook",
    "ToolHook",
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
