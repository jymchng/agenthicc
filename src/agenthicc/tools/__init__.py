"""Agenthicc tool execution layer and lifecycle hooks (PRD-04)."""

from .base import Tool, ToolResultEnvelope
from .executor import TOOL_ENTITY, AgenthiccToolExecutor, PermissionChecker
from .hooks import (
    HookRegistry,
    HookRunner,
    LaurenToolHookAdapter,
    LifecycleHook,
    RecoveryAction,
    Rejection,
    load_hook_from_dotpath,
)
from .sandbox import NetworkGuard, WorkspaceView

__all__ = [
    "AgenthiccToolExecutor",
    "HookRegistry",
    "HookRunner",
    "LaurenToolHookAdapter",
    "LifecycleHook",
    "NetworkGuard",
    "PermissionChecker",
    "RecoveryAction",
    "Rejection",
    "TOOL_ENTITY",
    "Tool",
    "ToolResultEnvelope",
    "WorkspaceView",
    "load_hook_from_dotpath",
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
